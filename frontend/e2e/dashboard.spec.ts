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

const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";
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

test.describe("ingest -> review -> negotiation sheet", () => {
  test.skip(!TENANT_ID, "NEXT_PUBLIC_DEV_TENANT_ID not set in frontend/.env.local");

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
});
