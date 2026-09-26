import { NextResponse, type NextRequest } from "next/server";

/** Sends anyone without a session cookie to sign in before a page renders.
 *
 * Only checks that the cookie exists; whether it's still valid is the API's
 * call, made on every request (an expired or revoked session gets a 401, and
 * the page redirects then). The API proxy is excluded: it answers for itself.
 */
export function middleware(request: NextRequest) {
  if (request.cookies.has("ii_session")) return NextResponse.next();
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  url.search = `?next=${encodeURIComponent(request.nextUrl.pathname + request.nextUrl.search)}`;
  return NextResponse.redirect(url);
}

export const config = {
  matcher: ["/((?!login|logout|api/|_next/|favicon\\.ico).*)"],
};
