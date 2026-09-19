import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // Playwright owns e2e/*.spec.ts (frontend/playwright.config.ts) — without
    // this, vitest's default glob also picks those files up and fails trying
    // to run Playwright's test.describe() in vitest's runner.
    exclude: ["e2e/**", "node_modules/**"],
  },
});
