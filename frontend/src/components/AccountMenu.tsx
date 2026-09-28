"use client";

import { CHANGE_PASSWORD_PATH } from "@/lib/api";
import type { Location } from "@/lib/server";

const LOCATION_COOKIE = "ii_location";

/** Location switcher, the signed-in user, and sign out. */
export default function AccountMenu({
  name,
  locations,
  locationId,
}: {
  name: string;
  locations: Location[];
  locationId: string | null;
}) {
  function switchTo(id: string) {
    // Secure when the page is: over https, a cookie without it can be set or
    // replaced by anything on the network path over plain http.
    const secure = window.location.protocol === "https:" ? "; secure" : "";
    document.cookie = `${LOCATION_COOKIE}=${id}; path=/; max-age=${60 * 60 * 24 * 365}; samesite=lax${secure}`;
    // A full reload, not router.refresh(): the client screens hold data
    // fetched for the previous location in their own state.
    window.location.reload();
  }

  return (
    <div className="ml-auto flex flex-none items-center gap-3 whitespace-nowrap text-sm">
      {locations.length > 1 ? (
        <select
          aria-label="Location"
          value={locationId ?? ""}
          onChange={(e) => switchTo(e.target.value)}
          className="input max-w-48 py-1.5"
        >
          {locations.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      ) : (
        <span className="rounded-md bg-brand-50 px-2.5 py-1 font-medium text-brand-800">{locations[0]?.name ?? "No locations"}</span>
      )}
      <a href={CHANGE_PASSWORD_PATH} title="Your account" className="font-medium text-gray-600 hover:text-brand-700">
        {name}
      </a>
      {/* A form, not a click handler: works before hydration (app/logout/route.ts). */}
      <form action="/logout" method="post">
        <button type="submit" className="btn-secondary btn-sm">
          Sign out
        </button>
      </form>
    </div>
  );
}
