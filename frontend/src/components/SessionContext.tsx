"use client";

import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect } from "react";

import { CHANGE_PASSWORD_PATH, isPublicPath, loginPath } from "@/lib/api";
import type { Session } from "@/lib/server";


const SessionContext = createContext<Session | null>(null);

/** Hands the server-resolved session to client components, so they don't each
 *  re-ask the API who is signed in and which location is selected.
 *
 *  No session outside the signed-out pages (sign in, password reset) means
 *  the cookie is there but no longer valid
 *  (expired, revoked): middleware only checks that it exists. Server pages
 *  redirect on their own; this covers the client-rendered ones, which would
 *  otherwise say "no locations" to someone who simply needs to sign in again.
 */
export function SessionProvider({ session, children }: { session: Session | null; children: React.ReactNode }) {
  const pathname = usePathname();
  const publicPage = isPublicPath(pathname);
  const signedOut = !session && !publicPage;
  // Client-rendered pages too: someone holding an operator-issued password
  // goes to replace it first (the API refuses everything else until then).
  const mustChange = !!session?.user.password_change_required && pathname !== CHANGE_PASSWORD_PATH && !publicPage;
  useEffect(() => {
    if (signedOut) window.location.replace(loginPath(window.location.pathname + window.location.search));
    else if (mustChange) window.location.replace(CHANGE_PASSWORD_PATH);
  }, [signedOut, mustChange]);
  if (signedOut || mustChange) return null;
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}

export function useSession(): Session | null {
  return useContext(SessionContext);
}

/** The selected location's id, or "" when the user has none. */
export function useLocationId(): string {
  return useSession()?.locationId ?? "";
}
