import { execFileSync } from "node:child_process";
import path from "node:path";
import { expect, test } from "@playwright/test";

// SPEC.md §10 Phase 5 gate: ingest -> review -> negotiation sheet, headless,
// no shell commands beyond `npx playwright test` itself. "Ingest" here seeds
// two pending review-queue line items directly (via backend/scripts/
// e2e_fixture.py) instead of uploading a PDF through real vision extraction —
// the same "ground truth stands in for extraction" reasoning
// seed_corpus_pipeline.py already uses for Phase 4, so this gate doesn't
// depend on Anthropic API budget/credit to run.

const TENANT_ID = process.env.E2E_TENANT_ID ?? "";
const CSRF = { "X-Requested-With": "invoice-intelligence" };
const REPO_ROOT = path.resolve(__dirname, "../..");
const BACKEND_DIR = path.join(REPO_ROOT, "backend");
const PYTHON = path.join(BACKEND_DIR, ".venv/bin/python");
const FIXTURE_SCRIPT = path.join(BACKEND_DIR, "scripts/e2e_fixture.py");

type FixtureSetup = {
  distributor_id: string;
  confirm_line_id: string;
  confirm_raw_description: string;
  correct_line_id: string;
  correct_raw_sku: string;
  correct_raw_description: string;
};

type VerifyAliasResult = {
  method: string;
  review_status: string;
  canonical_sku_id: string | null;
  match_confidence: string | null;
};

function runFixture(...args: string[]) {
  const out = execFileSync(PYTHON, [FIXTURE_SCRIPT, ...args], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: BACKEND_DIR },
  });
  return JSON.parse(out.toString());
}

type Login = { email: string; password: string };

// A throwaway member of TENANT_ID, with a password generated for this run
// (backend/scripts/e2e_fixture.py login). Made once: re-running it resets the
// password and signs out every session it had.
let login: Login | null = null;
function e2eLogin(): Login {
  login ??= runFixture("login", TENANT_ID) as Login;
  return login;
}

test.describe("signing in", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test("a signed-out visitor is sent to sign in, and back where they were after", async ({ page }) => {
    const { email, password } = e2eLogin();
    await page.goto("/negotiation");
    await expect(page).toHaveURL(/\/login\?next=%2Fnegotiation/);

    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("not the password");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("That email and password don't match.")).toBeVisible();

    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/negotiation$/);
    await expect(page.getByText("E2E Reviewer")).toBeVisible();
    // A member, not an operator: no cross-location Businesses page.
    await expect(page.getByRole("link", { name: "Businesses" })).toHaveCount(0);

    await page.getByRole("button", { name: "Sign out" }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/invoices");
    await expect(page).toHaveURL(/\/login/);
  });
});

test.describe("ingest -> review -> negotiation sheet", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test.beforeEach(async ({ page }) => {
    const res = await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF });
    expect(res.ok()).toBeTruthy();
  });

  // Fixtures confirm invoices on real catalog SKUs, so they'd otherwise sit in
  // this tenant's real benchmarks and creep alerts until the next run.
  test.afterAll(() => {
    if (TENANT_ID) runFixture("cleanup", TENANT_ID);
  });

  test("review queue confirms and corrects entirely by keyboard, and a correction auto-resolves the next matching line", async ({
    page,
  }) => {
    const fixture = runFixture("setup", TENANT_ID) as FixtureSetup;
    const searchInput = () => page.getByPlaceholder("Search products, e.g. mozzarella");

    await page.goto("/review");

    // Fixture items sort first in the queue (see e2e_fixture.py) — handle
    // both regardless of which one the queue shows first.
    for (let i = 0; i < 2; i++) {
      const current = await page.getByTestId("review-description").textContent();

      if (current?.includes(fixture.confirm_raw_description)) {
        await expect(page.getByText("Press", { exact: false })).toBeVisible();
        await searchInput().press("Enter"); // confirm: empty search box, Enter
      } else if (current?.includes(fixture.correct_raw_description)) {
        await searchInput().fill("avocado");
        await expect(page.getByText("Avocado", { exact: false }).first()).toBeVisible({ timeout: 5_000 });
        await searchInput().press("Enter"); // correct: search + Enter
      } else {
        throw new Error(`Unexpected review queue item: ${current}`);
      }

      await expect(page.getByTestId("review-item")).not.toContainText(current ?? "__none__");
    }

    // "The next matching line auto-resolves": the correction wrote a
    // sku_aliases row, so calling the matcher again for the same
    // (distributor, raw_sku) should now short-circuit via the alias.
    const verify = runFixture(
      "verify-alias",
      TENANT_ID,
      fixture.distributor_id,
      fixture.correct_raw_sku,
      fixture.correct_raw_description
    ) as VerifyAliasResult;
    expect(verify.method).toBe("alias");
    expect(verify.review_status).toBe("auto");
  });

  test("clears 20 review-queue items via keyboard in well under two minutes", async ({ page }) => {
    // SPEC.md §10 Phase 5 demo line: "clear 20 review-queue items in under
    // two minutes." All 20 already have a suggested match (the review-band
    // case, which is most of a real queue) so this measures the confirm
    // path's raw keyboard throughput.
    const bulk = runFixture("setup-bulk", TENANT_ID, "20") as { distributor_id: string; line_ids: string[] };

    // Scoped to just this batch's distributor — without this, the loop below
    // would race against whatever else is already pending for this tenant
    // (real corpus review items, leftover fixtures from prior runs) and
    // undercount, since pressing Enter on an item with no suggested match is
    // correctly a no-op.
    await page.goto(`/review?distributor_id=${bulk.distributor_id}`);
    const searchInput = page.getByPlaceholder("Search products, e.g. mozzarella");
    await expect(page.getByTestId("review-description")).toBeVisible({ timeout: 10_000 });

    const start = Date.now();
    for (let i = 1; i <= 20; i++) {
      await searchInput.press("Enter");
      // Wait for the actual advance (a real state transition) rather than
      // assuming .press() serializes against React's async confirm() call —
      // a rapid-fire loop otherwise risks pressing Enter again before the
      // in-flight confirm's busy state has re-rendered. The counter text
      // works uniformly for every iteration, including the last one, where
      // the queue empties out and the "current item" locator disappears
      // entirely (a case a text-diff wait on that locator can't express).
      await expect(page.getByText(new RegExp(`${i} done|You matched ${i} items?\\b`))).toBeVisible({
        timeout: 10_000,
      });
    }
    const elapsedSeconds = (Date.now() - start) / 1000;

    expect(elapsedSeconds).toBeLessThan(120);
    console.log(`Cleared 20 review-queue items in ${elapsedSeconds.toFixed(1)}s`);
  });

  test("negotiation sheet renders a ranked, printable table", async ({ page }) => {
    await page.goto("/negotiation");
    await expect(page.getByRole("heading", { name: "Savings", exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Print" })).toBeVisible();
    await expect(page.getByRole("table").or(page.getByText("Nothing to save on yet"))).toBeVisible({
      timeout: 10_000,
    });
  });

  test("negotiation sheet can be argued from the tenant's own history alone", async ({ page }) => {
    // The day-one path: no peer benchmark needed, so this basis must render a
    // sheet for a tenant whose metro has nobody else in it.
    await page.goto("/negotiation");
    await page.getByRole("button", { name: "What you used to pay" }).click();

    await expect(page.getByRole("table").or(page.getByText("Nothing to save on yet"))).toBeVisible({
      timeout: 10_000,
    });
    const rows = page.getByRole("row");
    if ((await rows.count()) > 1) {
      // Every line says where its target came from — the sheet must never put
      // a peer claim and a history claim under one unqualified header.
      await expect(page.getByText("your usual price").first()).toBeVisible();
      await expect(page.getByText(/scaled up to a year/)).toBeVisible();
    }
  });

  test("an invoice that doesn't add up can be corrected and confirmed back into analytics", async ({ page }) => {
    // Line 1's unit price was misread as $74.50; the page says $47.50. Until
    // someone fixes it, the invoice is held out of every benchmark.
    const fixture = runFixture("setup-needs-review", TENANT_ID) as { invoice_id: string; invoice_number: string };

    await page.goto(`/invoices/${fixture.invoice_id}`);
    await expect(page.getByText("Some numbers on this invoice don't add up")).toBeVisible();
    const confirm = page.getByRole("button", { name: "Confirm invoice" });
    await expect(confirm).toBeDisabled();

    await page.getByLabel("Line 1 unit price").fill("47.50");
    // The check on screen is of the SAVED numbers, so confirming must wait.
    await expect(confirm).toBeDisabled();
    await page.getByRole("button", { name: "Save and check" }).click();

    await expect(page.getByText("Everything adds up now")).toBeVisible();
    await confirm.click();

    await expect(page.getByText("Confirmed. Its prices now count")).toBeVisible();
    await expect(page.getByText("Confirmed", { exact: true })).toBeVisible();

    // The audit trail: who fixed which number, from what to what.
    const history = page.locator("section", { has: page.getByRole("heading", { name: "History" }) });
    await expect(history.getByText("E2E Reviewer confirmed the invoice")).toBeVisible();
    await expect(history.getByText("item 1: price each 74.50 → 47.50")).toBeVisible();
  });

  test("lines can be entered by hand when extraction found none, starting from a past purchase", async ({ page }) => {
    // `search`: a word from something this location really bought from the
    // invoice's distributor. Searching a fixed word ("chicken") assumed a
    // purchase history that differs by location, and CI's location varied.
    const fixture = runFixture("setup-empty-invoice", TENANT_ID) as { invoice_id: string; search: string };

    await page.goto(`/invoices/${fixture.invoice_id}`);
    await expect(page.getByText(/We didn.t find any items/)).toBeVisible();
    await page.getByRole("button", { name: "+ Add item" }).click();

    const dialog = page.getByRole("dialog", { name: "Add an item" });
    await dialog.getByLabel("Search past purchases").fill(fixture.search.toLowerCase());
    await dialog.getByRole("button").filter({ hasText: new RegExp(fixture.search, "i") }).first().click();

    // Identity comes from the past purchase; the price deliberately doesn't.
    // A prefilled price left untouched would record "no increase" and hide
    // exactly the creep the product exists to catch.
    await expect(dialog.getByLabel("Description")).not.toHaveValue("");
    await expect(dialog.getByLabel("Price each", { exact: true })).toHaveValue("");
    await expect(dialog.getByText(/Last paid/)).toBeVisible();

    await dialog.getByLabel("Quantity").fill("1");
    await dialog.getByLabel("Price each", { exact: true }).fill("612.00");
    await dialog.getByLabel("Line total").fill("612.00");
    await dialog.getByRole("button", { name: "Add item" }).click();
    await expect(dialog).toBeHidden();

    for (const [label, value] of [["Subtotal", "612.00"], ["Tax", "0.00"], ["Total", "612.00"]]) {
      await page.getByLabel(label, { exact: true }).fill(value);
    }
    await page.getByRole("button", { name: "Save and check" }).click();
    await page.getByRole("button", { name: "Confirm invoice" }).click();

    await expect(page.getByText("Confirmed. Its prices now count")).toBeVisible();
  });
});


test.describe("managing users", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");
  test.afterAll(() => {
    runFixture("cleanup-users");
  });

  test("an operator creates a login, hands over the password, and can deactivate it", async ({ page, browser }) => {
    const operator = runFixture("login-operator") as Login;
    expect((await page.request.post("/api/auth/login", { data: operator, headers: CSRF })).ok()).toBeTruthy();

    await page.goto("/users");
    await page.getByRole("button", { name: "Add person" }).click();
    const email = `e2e-new-${Date.now()}@dev.test`;
    await page.getByLabel("Name").fill("E2E New Person");
    await page.getByLabel("Email").fill(email);
    await page.getByRole("group", { name: "Locations" }).getByRole("checkbox").first().check();
    await page.getByRole("button", { name: "Create login" }).click();

    const shown = page.getByTestId("generated-password");
    await expect(shown).toBeVisible();
    const password = (await shown.textContent())!.trim();
    expect(password.length).toBeGreaterThanOrEqual(12);
    const row = page.getByRole("row", { name: new RegExp(email) });
    await expect(row).toBeVisible();

    // The new person signs in with it, in a browser of their own.
    const theirs = await browser.newContext({ baseURL: test.info().project.use.baseURL });
    const newcomer = await theirs.newPage();
    await newcomer.goto("/login");
    await newcomer.getByLabel("Email").fill(email);
    await newcomer.getByLabel("Password").fill(password);
    await newcomer.getByRole("button", { name: "Sign in" }).click();

    // The operator saw that password, so the first thing is to replace it,
    // and nothing else opens until they do.
    await expect(newcomer).toHaveURL(/\/account\/password$/);
    await newcomer.goto("/insights");
    await expect(newcomer).toHaveURL(/\/account\/password$/);
    await newcomer.getByLabel("Temporary password").fill(password);
    await newcomer.getByLabel("New password", { exact: true }).fill("a phrase of my own choosing");
    await newcomer.getByLabel("New password again").fill("a phrase of my own choosing");
    await newcomer.getByRole("button", { name: "Set password and continue" }).click();
    await expect(newcomer).toHaveURL(/\/invoices$/);
    await expect(newcomer.getByText("E2E New Person")).toBeVisible();

    // Deactivating signs them out at once.
    page.once("dialog", (dialog) => void dialog.accept());
    await row.getByRole("button", { name: "Deactivate" }).click();
    await expect(row.getByText("Deactivated")).toBeVisible();
    await newcomer.reload();
    await expect(newcomer).toHaveURL(/\/login/);
    await theirs.close();

    // All of it is in the operators' audit log, newest first.
    await page.goto("/audit");
    await page.getByLabel("What").selectOption({ label: "People and access" });
    await expect(page.getByText(`E2E Operator deactivated ${email}`)).toBeVisible();
    await expect(page.getByText(`E2E Operator created a login for ${email}`)).toBeVisible();
  });
});

test.describe("operators' screens", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");
  test.afterAll(() => {
    runFixture("cleanup-locations");
    runFixture("cleanup-users");
  });

  test.beforeEach(async ({ page }) => {
    const operator = runFixture("login-operator") as Login;
    expect((await page.request.post("/api/auth/login", { data: operator, headers: CSRF })).ok()).toBeTruthy();
  });

  test("adding a location shows where it forwards invoices, and a refused one keeps what was typed", async ({ page }) => {
    const name = `E2E Location ${Date.now()}`;
    await page.goto("/accounts");
    await page.getByLabel("Location name").fill(name);
    await page.getByLabel("Area").fill("Austin, TX");
    await page.getByLabel("Annual food spend").selectOption({ label: "$1M–$3M" });
    await page.getByRole("button", { name: "Add location" }).click();

    const slug = name.toLowerCase().replaceAll(" ", "-");
    await expect(page.getByRole("status")).toContainText(`Added ${name}`);
    await expect(page.getByRole("status")).toContainText(`${slug}@`);
    const row = page.getByRole("row", { name: new RegExp(name) });
    await expect(row).toContainText("Austin, TX");
    await expect(page.getByLabel("Location name")).toHaveValue("");

    // The server refuses the next one: the form must keep the name.
    await page.route("**/api/tenants", (route) =>
      route.request().method() === "POST"
        ? route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ detail: "try again" }) })
        : route.continue(),
    );
    await page.getByLabel("Location name").fill(`${name} Two`);
    await page.getByRole("button", { name: "Add location" }).click();
    await expect(page.getByText("try again")).toBeVisible();
    await expect(page.getByLabel("Location name")).toHaveValue(`${name} Two`);
  });

  test("the audit log pages back through everything without gaps or repeats", async ({ page }) => {
    // Enough events of one kind by one person: three logins created.
    for (let i = 0; i < 3; i++) {
      const resp = await page.request.post("/api/users", {
        data: { email: `e2e-new-page-${Date.now()}-${i}@dev.test`, name: `E2E Paging ${i}` },
        headers: CSRF,
      });
      expect(resp.ok()).toBeTruthy();
    }
    // Pages of two instead of a hundred, so paging happens.
    await page.route("**/api/audit?*", (route) => route.continue({ url: route.request().url().replace("limit=100", "limit=2") }));

    await page.goto("/audit");
    await page.getByLabel("What").selectOption({ label: "People and access" });
    await page.getByLabel("Who").selectOption({ label: "E2E Operator" });
    const entries = page.locator("main ol > li");
    await expect(entries).toHaveCount(2);
    const older = page.getByRole("button", { name: "Show older" });
    while (await older.isVisible()) {
      const before = await entries.count();
      await older.click();
      await expect(entries).not.toHaveCount(before);
    }
    const texts = await entries.allInnerTexts();
    const created = texts.filter((t) => t.includes("created a login for e2e-new-page-"));
    expect(created.length).toBeGreaterThanOrEqual(3);
    expect(new Set(texts).size).toBe(texts.length);
  });
});

test.describe("reading times", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");
  test.afterAll(() => {
    if (TENANT_ID) runFixture("cleanup", TENANT_ID);
  });

  test("history shows times in the reader's timezone, not the server's", async ({ browser }) => {
    // A zone this machine (the server) is unlikely to be in.
    const zone = "Asia/Tokyo";
    const context = await browser.newContext({ baseURL: test.info().project.use.baseURL, timezoneId: zone });
    const page = await context.newPage();
    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
    const fixture = runFixture("setup-needs-review", TENANT_ID) as { invoice_id: string };

    await page.goto(`/invoices/${fixture.invoice_id}`);
    await page.getByLabel("Line 1 unit price").fill("47.50");
    await page.getByRole("button", { name: "Save and check" }).click();

    const history = page.locator("section", { has: page.getByRole("heading", { name: "History" }) });
    const stamp = history.locator("time[data-local]").first();
    await expect(stamp).toBeVisible();
    const iso = await stamp.getAttribute("datetime");
    const expected = new Date(iso!).toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZone: zone,
    });
    await expect(stamp).toHaveText(expected);
    await context.close();
  });
});

test.describe("weekly email", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test("a link from the digest opens on the location it was about", async ({ page }) => {
    // An operator can open every location, so which one shows is the question.
    const operator = runFixture("login-operator") as Login;
    expect((await page.request.post("/api/auth/login", { data: operator, headers: CSRF })).ok()).toBeTruthy();

    await page.goto(`/insights?location=${TENANT_ID}`);
    await expect(page).toHaveURL(/\/insights$/);
    const cookies = await page.context().cookies();
    expect(cookies.find((c) => c.name === "ii_location")?.value).toBe(TENANT_ID);
    await expect(page.getByRole("navigation").getByRole("combobox")).toHaveValue(TENANT_ID);
  });

  test("the weekly summary can be turned off and on from the account page", async ({ page }) => {
    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
    await page.goto("/account/password");
    const toggle = page.getByLabel(/Weekly summary/);
    await expect(toggle).toBeChecked();
    const saved = page.waitForResponse((r) => r.url().includes("/api/auth/me") && r.request().method() === "PATCH");
    await toggle.uncheck();
    await expect(toggle).not.toBeChecked();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    await expect(page.getByLabel(/Weekly summary/)).not.toBeChecked();
    await page.getByLabel(/Weekly summary/).check();
    await expect(page.getByLabel(/Weekly summary/)).toBeChecked();
  });
});

test.describe("getting invoices in and out", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test.beforeEach(async ({ page }) => {
    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
  });

  // A 1x1 PNG: the browser only needs something to preview; the server,
  // which does read photos, never sees these (the upload is intercepted, so
  // nothing reaches real extraction).
  const PIXEL = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
    "base64",
  );
  const photo = (name: string) => ({ name, mimeType: "image/png", buffer: PIXEL });

  test("photos of a paper invoice are gathered as pages and sent as one invoice", async ({ page }) => {
    let sentFiles: string[] = [];
    await page.route("**/api/invoices?tenant_id=*", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const body = route.request().postDataBuffer()?.toString("latin1") ?? "";
      sentFiles = [...body.matchAll(/name="file"; filename="([^"]+)"/g)].map((m) => m[1]);
      await route.fulfill({ status: 201, contentType: "application/json", body: '{"id":"x","status":"received"}' });
    });

    await page.goto("/invoices");
    await page.getByTestId("upload-input").setInputFiles([photo("page-1.png"), photo("page-2.png")]);
    const tray = page.getByTestId("photo-tray");
    await expect(tray.getByText("2 pages", { exact: true })).toBeVisible();

    await tray.getByRole("button", { name: "Remove page 2" }).click();
    await expect(tray.getByText("1 page", { exact: true })).toBeVisible();
    await page.getByTestId("add-page-input").setInputFiles([photo("page-2-retake.png")]);
    await expect(tray.getByText("2 pages", { exact: true })).toBeVisible();

    await tray.getByRole("button", { name: "Upload invoice (2 pages)" }).click();
    await expect(page.getByText("Uploaded. Reading the invoice now.")).toBeVisible();
    await expect(tray).toHaveCount(0);
    expect(sentFiles).toEqual(["page-1.png", "page-2-retake.png"]);
  });

  test("a PDF and photos together are refused before anything is sent", async ({ page }) => {
    await page.goto("/invoices");
    await page
      .getByTestId("upload-input")
      .setInputFiles([{ name: "invoice.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4") }, photo("p.png")]);
    await expect(page.getByText("Choose PDFs or photos, not both.")).toBeVisible();
    await expect(page.getByTestId("photo-tray")).toHaveCount(0);
  });

  test("the export is an Excel file unless CSV is asked for", async ({ page }) => {
    await page.goto("/invoices");
    await page.getByRole("button", { name: "Export" }).click();
    const downloading = page.waitForEvent("download");
    await page.getByTestId("export-panel").getByRole("link", { name: "Download Excel file" }).click();
    const download = await downloading;
    expect(download.suggestedFilename()).toMatch(/^invoices-.+\.xlsx$/);
  });

  test("line items export as a CSV Excel can read", async ({ page }) => {
    await page.goto("/invoices");
    await page.getByRole("button", { name: "Export" }).click();
    const panel = page.getByTestId("export-panel");
    await panel.getByText("Line items").click();
    await panel.getByText("CSV", { exact: true }).click();
    await panel.getByLabel("Invoice dates").selectOption("all");
    await expect(panel.getByTestId("export-range")).toHaveText("Every invoice");

    const downloading = page.waitForEvent("download");
    await panel.getByRole("link", { name: "Download CSV" }).click();
    const download = await downloading;
    expect(download.suggestedFilename()).toMatch(/^line-items-.+-all\.csv$/);
    const text = (await download.createReadStream().then(async (stream) => {
      const chunks: Buffer[] = [];
      for await (const chunk of stream) chunks.push(chunk as Buffer);
      return Buffer.concat(chunks);
    })).toString("utf-8");
    expect(text.startsWith("﻿Location,Invoice #,Invoice date,Distributor,Line,")).toBeTruthy();
    expect(text.split("\r\n").length).toBeGreaterThan(2); // the seeded location has lines
  });
});

test.describe("forgot your password", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  // The reset login (a member of the e2e location, which would otherwise get
  // its emails and sit on the Users screen) and the requests' history.
  test.afterAll(() => {
    if (TENANT_ID) runFixture("cleanup-users");
  });

  test("asking for a link says the same thing for any address", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("link", { name: "Forgot your password?" }).click();
    await expect(page).toHaveURL(/\/forgot-password$/);
    await page.getByLabel("Email").fill("nobody-e2e@test.invalid");
    await page.getByRole("button", { name: "Send reset link" }).click();
    await expect(page.getByRole("status")).toContainText("If nobody-e2e@test.invalid has an account");
  });

  test("the emailed link sets a new password and signs you in", async ({ page }) => {
    const { token } = runFixture("reset-link", TENANT_ID) as { email: string; token: string };
    await page.goto(`/reset-password?token=${encodeURIComponent(token)}`);
    // The token leaves the address bar as soon as the page has it.
    await expect(page).toHaveURL(/\/reset-password$/);
    const newPassword = `e2e ${Date.now()} new passphrase`;
    await page.getByLabel("New password", { exact: true }).fill(newPassword);
    await page.getByLabel("New password again").fill(newPassword);
    await page.getByRole("button", { name: "Set password and sign in" }).click();
    await expect(page).toHaveURL(/\/invoices$/);
    await expect(page.getByText("E2E Reset")).toBeVisible();

    // Used: the same link now says so.
    await page.context().clearCookies();
    await page.goto(`/reset-password?token=${encodeURIComponent(token)}`);
    await expect(page.getByRole("heading", { name: "This link doesn’t work any more" })).toBeVisible();
  });
});

test.describe("price-increase emails", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test("can be turned off without turning off the weekly summary", async ({ page }) => {
    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
    await page.goto("/account/password");
    const alerts = page.getByLabel(/Price increases, as they happen/);
    await expect(alerts).toBeChecked();
    const saved = page.waitForResponse((r) => r.url().includes("/api/auth/me") && r.request().method() === "PATCH");
    await alerts.uncheck();
    expect((await saved).ok()).toBeTruthy();
    await page.reload();
    await expect(page.getByLabel(/Price increases, as they happen/)).not.toBeChecked();
    await expect(page.getByLabel(/Weekly summary/)).toBeChecked();
  });
});

test.describe("finding your way", () => {
  test.skip(!TENANT_ID, "E2E_TENANT_ID not set in frontend/.env.local");

  test("help is open to everyone, and every link on it leads somewhere", async ({ page }) => {
    await page.goto("/login");
    await page.getByRole("link", { name: "Help" }).click();
    await expect(page).toHaveURL(/\/help$/);
    await expect(page.getByRole("heading", { name: "Help", exact: true })).toBeVisible();

    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
    await page.goto("/help");
    const hrefs = await page.locator("main a[href]").evaluateAll((as) => as.map((a) => a.getAttribute("href")!));
    for (const href of new Set(hrefs)) {
      if (href.startsWith("#")) {
        await expect(page.locator(href), `anchor ${href}`).toHaveCount(1);
      } else {
        expect((await page.request.get(href)).status(), href).toBe(200);
      }
    }
    await page.getByRole("link", { name: "Match items" }).first().click();
    await expect(page).toHaveURL(/\/review$/);
  });

  test("an item you can't match can be skipped", async ({ page }) => {
    const fixture = runFixture("setup", TENANT_ID) as FixtureSetup;
    expect((await page.request.post("/api/auth/login", { data: e2eLogin(), headers: CSRF })).ok()).toBeTruthy();
    await page.goto(`/review?distributor_id=${fixture.distributor_id}`);
    const first = await page.getByTestId("review-description").textContent();
    await expect(page.getByText(/^1 of 2/)).toBeVisible();
    await page.getByRole("button", { name: "Skip for now" }).click();
    await expect(page.getByText(/^2 of 2/)).toBeVisible();
    await expect(page.getByTestId("review-description")).not.toHaveText(first ?? "");
    await page.getByRole("button", { name: "Skip for now" }).click();
    await expect(page.getByText("2 skipped items are still waiting for next time.", { exact: false })).toBeVisible();
    await expect(page.getByRole("link", { name: "See price alerts" })).toBeVisible();
    runFixture("cleanup", TENANT_ID);
  });
});
