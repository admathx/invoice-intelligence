import { NextResponse, type NextRequest } from "next/server";

const LOCATION_COOKIE = "ii_location";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Sends anyone without a session cookie to sign in before a page renders,
 *  and honours ?location= on links from emails.
 *
 * Only checks that the session cookie exists; whether it's still valid is the
 * API's call, made on every request (an expired or revoked session gets a
 * 401, and the page redirects then). The API proxy is excluded: it answers
 * for itself.
 *
 * ?location=<id> (the weekly digest's links carry it) selects that location
 * and redirects to the same page without it, so a link from one location's
 * section of the email never opens in another's. It's only a preference: the
 * API still decides access, so a location you can't open just isn't shown.
 */
export function middleware(request: NextRequest) {
  if (!request.cookies.has("ii_session")) {
    const url = request.nextUrl.clone();
    url.pathname = "/login";
    // The query, ?location= included, comes back after sign-in.
    url.search = `?next=${encodeURIComponent(request.nextUrl.pathname + request.nextUrl.search)}`;
    return NextResponse.redirect(url);
  }
  const location = request.nextUrl.searchParams.get("location");
  if (location !== null) {
    const url = request.nextUrl.clone();
    url.searchParams.delete("location");
    const response = NextResponse.redirect(url);
    if (UUID.test(location)) {
      response.cookies.set(LOCATION_COOKIE, location, {
        path: "/",
        maxAge: 60 * 60 * 24 * 365,
        sameSite: "lax",
        secure: request.nextUrl.protocol === "https:" || request.headers.get("x-forwarded-proto") === "https",
      });
    }
    return response;
  }
  return NextResponse.next();
}

export const config = {
  // The signed-out pages: signing in, getting back in without a password, help
  // (lib/api.ts isPublicPath, which the client-side session check uses).
  matcher: ["/((?!login|logout|forgot-password|reset-password|help|api/|_next/|favicon\\.ico).*)"],
};
