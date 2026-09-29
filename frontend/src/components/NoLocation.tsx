"use client";

import { useSession } from "./SessionContext";

/** Shown when there's no location to show. For an operator that means none
 *  exist yet (a fresh deployment), and the next step is theirs to take; for
 *  anyone else, it means nobody has given them one. */
export default function NoLocation() {
  if (useSession()?.user.is_operator) {
    return (
      <p className="text-sm text-gray-600">
        There are no locations yet. Add the first one on the{" "}
        <a href="/accounts" className="link">
          Businesses
        </a>{" "}
        page, then give people access to it on the{" "}
        <a href="/users" className="link">
          People
        </a>{" "}
        page.
      </p>
    );
  }
  return (
    <p className="text-sm text-gray-600">
      You don&rsquo;t have access to a location yet. Ask whoever set up your account to add you to one.
    </p>
  );
}
