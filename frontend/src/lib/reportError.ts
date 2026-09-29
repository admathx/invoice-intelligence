import { CSRF_HEADERS } from "./api";

/** Tell whoever runs the service that a page broke (backend app/api/ops.py).
 *  Best effort: reporting must never become a second error. Only signed-in
 *  people's reports are accepted, so for anyone else this quietly does
 *  nothing. */
export function reportError(error: Error & { digest?: string }): void {
  try {
    void fetch("/api/ops/client-error", {
      method: "POST",
      headers: { ...CSRF_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify({
        message: String(error?.message ?? error).slice(0, 2000),
        page: window.location.pathname.slice(0, 500),
        digest: error?.digest ?? null,
      }),
      keepalive: true,
    }).catch(() => {});
  } catch {
    // nothing to do
  }
}
