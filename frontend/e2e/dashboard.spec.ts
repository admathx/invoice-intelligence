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
    await expect(page.getByText("email or password is incorrect")).toBeVisible();

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
    const searchInput = () => page.getByPlaceholder("Search canonical SKU to correct...");

    await page.goto("/review");

    // Fixture items sort first in the queue (see e2e_fixture.py) — handle
    // both regardless of which one the queue shows first.
    for (let i = 0; i < 2; i++) {
      const current = await page.locator(".rounded.border.border-gray-200.bg-white.p-4 .text-lg").textContent();

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

      await expect(page.locator(".rounded.border.border-gray-200.bg-white.p-4")).not.toContainText(current ?? "__none__");
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
    const searchInput = page.getByPlaceholder("Search canonical SKU to correct...");
    await expect(page.locator(".text-lg.font-medium")).toBeVisible({ timeout: 10_000 });

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
      await expect(page.getByText(new RegExp(`${i} cleared|Cleared ${i} items?\\b`))).toBeVisible({
        timeout: 10_000,
      });
    }
    const elapsedSeconds = (Date.now() - start) / 1000;

    expect(elapsedSeconds).toBeLessThan(120);
    console.log(`Cleared 20 review-queue items in ${elapsedSeconds.toFixed(1)}s`);
  });

  test("negotiation sheet renders a ranked, printable table", async ({ page }) => {
    await page.goto("/negotiation");
    await expect(page.getByRole("heading", { name: "Negotiation sheet" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Print" })).toBeVisible();
    await expect(page.getByRole("table").or(page.getByText("No overpriced SKUs"))).toBeVisible({
      timeout: 10_000,
    });
  });

  test("negotiation sheet can be argued from the tenant's own history alone", async ({ page }) => {
    // The day-one path: no peer benchmark needed, so this basis must render a
    // sheet for a tenant whose metro has nobody else in it.
    await page.goto("/negotiation");
    await page.getByRole("button", { name: "Your own history" }).click();

    await expect(page.getByRole("table").or(page.getByText("No overpriced SKUs"))).toBeVisible({
      timeout: 10_000,
    });
    const rows = page.getByRole("row");
    if ((await rows.count()) > 1) {
      // Every line says where its target came from — the sheet must never put
      // a peer claim and a history claim under one unqualified header.
      await expect(page.getByText("your median").first()).toBeVisible();
      await expect(page.getByText(/annualized at/)).toBeVisible();
    }
  });

  test("an invoice that doesn't add up can be corrected and confirmed back into analytics", async ({ page }) => {
    // Line 1's unit price was misread as $74.50; the page says $47.50. Until
    // someone fixes it, the invoice is held out of every benchmark.
    const fixture = runFixture("setup-needs-review", TENANT_ID) as { invoice_id: string; invoice_number: string };

    await page.goto(`/invoices/${fixture.invoice_id}`);
    await expect(page.getByText("numbers don't add up")).toBeVisible();
    const confirm = page.getByRole("button", { name: "Confirm invoice" });
    await expect(confirm).toBeDisabled();

    await page.getByLabel("Line 1 unit price").fill("47.50");
    // The check on screen is of the SAVED numbers, so confirming must wait.
    await expect(confirm).toBeDisabled();
    await page.getByRole("button", { name: "Save & re-check" }).click();

    await expect(page.getByText("The numbers add up now")).toBeVisible();
    await confirm.click();

    await expect(page.getByText("Confirmed after review")).toBeVisible();
    await expect(page.getByText("confirmed", { exact: true })).toBeVisible();

    // The audit trail: who fixed which number, from what to what.
    const history = page.locator("section", { has: page.getByRole("heading", { name: "History" }) });
    await expect(history.getByText("E2E Reviewer confirmed the invoice")).toBeVisible();
    await expect(history.getByText("line 1: unit price 74.50 → 47.50")).toBeVisible();
  });

  test("lines can be entered by hand when extraction found none, starting from a past purchase", async ({ page }) => {
    const fixture = runFixture("setup-empty-invoice", TENANT_ID) as { invoice_id: string };

    await page.goto(`/invoices/${fixture.invoice_id}`);
    await expect(page.getByText("No line items were detected")).toBeVisible();
    await page.getByRole("button", { name: "+ Add line item" }).click();

    const dialog = page.getByRole("dialog", { name: "Add line item" });
    await dialog.getByLabel("Search past purchases").fill("chicken");
    await dialog.getByRole("button").filter({ hasText: /CHICKEN/ }).first().click();

    // Identity comes from the past purchase; the price deliberately doesn't.
    // A prefilled price left untouched would record "no increase" and hide
    // exactly the creep the product exists to catch.
    await expect(dialog.getByLabel("Description")).not.toHaveValue("");
    await expect(dialog.getByLabel("Unit price", { exact: true })).toHaveValue("");
    await expect(dialog.getByText(/Last paid/)).toBeVisible();

    await dialog.getByLabel("Quantity").fill("1");
    await dialog.getByLabel("Unit price", { exact: true }).fill("612.00");
    await dialog.getByLabel("Extended price").fill("612.00");
    await dialog.getByRole("button", { name: "Add line" }).click();
    await expect(dialog).toBeHidden();

    for (const [label, value] of [["subtotal", "612.00"], ["tax", "0.00"], ["total", "612.00"]]) {
      await page.getByLabel(label, { exact: true }).fill(value);
    }
    await page.getByRole("button", { name: "Save & re-check" }).click();
    await page.getByRole("button", { name: "Confirm invoice" }).click();

    await expect(page.getByText("Confirmed after review")).toBeVisible();
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
    await page.getByRole("button", { name: "Add user" }).click();
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
    await expect(newcomer.getByText("E2E New Person")).toBeVisible();

    // Deactivating signs them out at once.
    page.once("dialog", (dialog) => void dialog.accept());
    await row.getByRole("button", { name: "Deactivate" }).click();
    await expect(row.getByText("Deactivated")).toBeVisible();
    await newcomer.reload();
    await expect(newcomer).toHaveURL(/\/login/);
    await theirs.close();
  });
});
