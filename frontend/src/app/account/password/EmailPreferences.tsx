"use client";

import { useState } from "react";

import { api, jsonInit } from "@/lib/api";
import { useHydrated } from "@/lib/useHydrated";

/** The weekly email: on or off. Saved as soon as it's changed. */
export default function EmailPreferences({ initiallyEnabled }: { initiallyEnabled: boolean }) {
  const [enabled, setEnabled] = useState(initiallyEnabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hydrated = useHydrated();

  async function toggle(next: boolean) {
    // Shown at once, put back if the save fails: a checkbox that only moves
    // when the server answers looks, for that moment, like it ignored the click.
    setEnabled(next);
    setBusy(true);
    setError(null);
    try {
      const res = await api("/auth/me", jsonInit("PATCH", { digest_enabled: next }));
      if (!res.ok) {
        setEnabled(!next);
        setError("Couldn't save that. Try again.");
        return;
      }
      setEnabled((await res.json()).digest_enabled);
    } catch {
      setEnabled(!next);
      setError("Couldn't reach the server. Nothing was changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="max-w-sm">
      <h2 className="text-base font-semibold">Email</h2>
      <label className="mt-2 flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={enabled}
          disabled={!hydrated || busy}
          onChange={(e) => void toggle(e.target.checked)}
        />
        <span>
          Weekly summary
          <span className="block text-gray-500">
            Every Monday: new price increases, invoices to check, the review queue, and your biggest savings, for each of
            your locations. Nothing is sent in a quiet week.
          </span>
        </span>
      </label>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  );
}
