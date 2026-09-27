/** The browser's way to the API.
 *
 * Requests go to /api on the frontend's own origin, which next.config.js
 * proxies to the backend. Same origin means the session cookie is first-party
 * and there's no CORS to configure. Every request carries the header the API
 * requires on writes that ride the session cookie (see backend app/auth.py);
 * another site can't add it without a CORS preflight the API refuses.
 */
export const CSRF_HEADERS = { "X-Requested-With": "invoice-intelligence" } as const;

/** Where a signed-out user is sent, remembering where they were. */
export function loginPath(next: string): string {
  return `/login?next=${encodeURIComponent(next)}`;
}

/** Only a path on this site is a safe place to send someone after sign-in;
 *  anything else (another origin, "//evil.example") falls back to the default. */
export function safeNext(next: string | null | undefined, fallback = "/invoices"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\")) return fallback;
  if (next === "/login" || next.startsWith("/login?")) return fallback;
  return next;
}

export async function api(path: string, init: RequestInit = {}): Promise<Response> {
  const res = await fetch(`/api${path}`, {
    cache: "no-store",
    ...init,
    headers: { ...CSRF_HEADERS, ...(init.headers as Record<string, string> | undefined) },
  });
  if (typeof window !== "undefined") {
    if (res.status === 401) {
      // The session ended (signed out elsewhere, expired, deactivated).
      window.location.assign(loginPath(window.location.pathname + window.location.search));
    } else if (res.status === 403) {
      // An operator reset their password mid-session: replace it first.
      const detail = await res
        .clone()
        .json()
        .then((body) => body?.detail)
        .catch(() => null);
      if (detail === "password change required") window.location.assign("/account/password");
    }
  }
  return res;
}

/** JSON body helpers, since nearly every write sends one. */
export function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}
