import { readFileSync } from "node:fs";
import path from "node:path";
import { defineConfig } from "@playwright/test";

// Playwright doesn't read .env.local the way `next dev` does — load it here
// so e2e/dashboard.spec.ts can see E2E_TENANT_ID.
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
// The dashboard's port for the run. 3001 by default; set E2E_PORT to run a
// second copy beside a dev server (with API_BASE pointing at its own API).
const PORT = process.env.E2E_PORT ?? "3001";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  // One worker, always: the tests share the e2e logins (signing one in
  // resets its password) and the database, so parallel workers invalidate
  // each other's sessions. --repeat-each would otherwise spread repeats out.
  workers: 1,
  reporter: "list",
  use: {
    baseURL: `http://localhost:${PORT}`,
  },
  webServer: {
    command: `npm run dev -- -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
