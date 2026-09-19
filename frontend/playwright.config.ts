import { readFileSync } from "node:fs";
import path from "node:path";
import { defineConfig } from "@playwright/test";

// Playwright doesn't read .env.local the way `next dev` does — load it here
// so e2e/dashboard.spec.ts can see NEXT_PUBLIC_DEV_TENANT_ID.
try {
  const envPath = path.join(__dirname, ".env.local");
  for (const line of readFileSync(envPath, "utf-8").split("\n")) {
    const match = line.match(/^([A-Z0-9_]+)=(.*)$/);
    if (match && !(match[1] in process.env)) process.env[match[1]] = match[2];
  }
} catch {
  // .env.local is optional (e.g. CI) — tests that need it skip themselves.
}

// Requires `make up` already running (postgres, redis, the FastAPI API, and
// the RQ worker) — same assumption `make smoke` and `make validate` make.
// This config only launches the frontend itself.
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3001",
  },
  webServer: {
    command: "npm run dev -- -p 3001",
    url: "http://localhost:3001",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
