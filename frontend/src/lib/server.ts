/** The server components' way to the API, and who is signed in.
 *
 * Server components call the backend directly (not through the /api proxy)
 * and forward the browser's session cookie themselves. Only that cookie is
 * forwarded: nothing else the browser holds is the backend's business.
 */
import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { cache } from "react";

import { CHANGE_PASSWORD_PATH } from "./api";

export const BACKEND = process.env.API_BASE ?? "http://localhost:8000";
export const SESSION_COOKIE = "ii_session";
/** Which of the user's locations the screens show. Only a preference: the API
 *  checks access on every request, so a hand-edited value gets a 404, not data. */
export const LOCATION_COOKIE = "ii_location";

export type Location = { id: string; name: string; metro: string };
export type Me = {
  id: string;
  email: string;
  name: string;
  is_operator: boolean;
  /** An operator issued their password; nothing else works until they replace it. */
  password_change_required: boolean;
  /** Gets the weekly email. */
  digest_enabled: boolean;
  locations: Location[];
};


export type Session = { user: Me; locationId: string | null };

function sessionCookieHeader(): Record<string, string> {
  const session = cookies().get(SESSION_COOKIE);
  return session ? { cookie: `${SESSION_COOKIE}=${session.value}` } : {};
}

/** GET from the API as the signed-in user. A 401 means the session is gone,
 *  so the page goes to sign-in rather than rendering an empty state. */
export async function serverGet(path: string): Promise<Response> {
  const res = await fetch(`${BACKEND}${path}`, { cache: "no-store", headers: sessionCookieHeader() });
  if (res.status === 401) redirect("/login");
  return res;
}

/** Once per request (React cache), however many components ask. */
export const getSession = cache(async (): Promise<Session | null> => {
  if (!cookies().get(SESSION_COOKIE)) return null;
  const res = await fetch(`${BACKEND}/auth/me`, { cache: "no-store", headers: sessionCookieHeader() });
  if (!res.ok) return null;
  const user: Me = await res.json();
  const wanted = cookies().get(LOCATION_COOKIE)?.value;
  const locationId = user.locations.find((l) => l.id === wanted)?.id ?? user.locations[0]?.id ?? null;
  return { user, locationId };
});

export async function requireSession(): Promise<Session> {
  const session = await getSession();
  if (!session) redirect("/login");
  // The API refuses everything else until then anyway (backend app/auth.py);
  // this sends them where they can do something about it.
  if (session.user.password_change_required) redirect(CHANGE_PASSWORD_PATH);
  return session;
}
