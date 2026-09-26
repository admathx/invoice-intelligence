import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { BACKEND, SESSION_COOKIE } from "@/lib/server";

/** Sign out as a plain form POST, so it works before the page has hydrated
 *  (a button with a click handler silently did nothing if pressed too soon).
 *
 *  Revokes the session server-side, then drops the cookie. Refuses a POST
 *  from another origin: a forged sign-out is only a nuisance, but there's no
 *  reason to allow it.
 */
export async function POST(request: Request) {
  const origin = request.headers.get("origin");
  if (origin && origin !== new URL(request.url).origin) {
    return new NextResponse("cross-origin sign-out refused", { status: 403 });
  }
  const session = cookies().get(SESSION_COOKIE);
  if (session) {
    await fetch(`${BACKEND}/auth/logout`, {
      method: "POST",
      headers: { cookie: `${SESSION_COOKIE}=${session.value}`, "X-Requested-With": "invoice-intelligence" },
    }).catch(() => null); // the cookie still goes; the session expires on its own
  }
  // 303: the browser follows with a GET, not a re-POST.
  const response = NextResponse.redirect(new URL("/login", request.url), 303);
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
