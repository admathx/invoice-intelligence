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
/** The address the browser used. Behind the HTTPS proxy (deploy/Caddyfile)
 *  this server itself is reached over plain http, so request.url says http
 *  and the forwarded headers say what the browser actually saw. */
function publicOrigin(request: Request): URL {
  const url = new URL(request.url);
  const proto = request.headers.get("x-forwarded-proto")?.split(",")[0].trim() || url.protocol.replace(":", "");
  const host = request.headers.get("x-forwarded-host")?.split(",")[0].trim() || request.headers.get("host") || url.host;
  return new URL(`${proto}://${host}`);
}

export async function POST(request: Request) {
  const here = publicOrigin(request);
  const origin = request.headers.get("origin");
  // Compared by host: the scheme is exactly what a proxy can make disagree.
  if (origin && new URL(origin).host !== here.host) {
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
  const response = NextResponse.redirect(new URL("/login", here), 303);
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
