"use client";

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
    <div className="ml-auto flex items-center gap-3 whitespace-nowrap text-sm">
      {locations.length > 1 ? (
        <select
          aria-label="Location"
          value={locationId ?? ""}
          onChange={(e) => switchTo(e.target.value)}
          className="max-w-56 rounded border border-gray-300 bg-white px-2 py-1 text-sm"
        >
          {locations.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      ) : (
        <span className="text-gray-600">{locations[0]?.name ?? "No locations"}</span>
      )}
      <a href="/account/password" title="Change your password" className="text-gray-400 hover:text-gray-900">
        {name}
      </a>
      {/* A form, not a click handler: works before hydration (app/logout/route.ts). */}
      <form action="/logout" method="post">
        <button type="submit" className="text-gray-600 hover:text-gray-900">
          Sign out
        </button>
      </form>
    </div>
  );
}
