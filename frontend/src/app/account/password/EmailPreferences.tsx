"use client";

import { useState } from "react";

import { api, jsonInit } from "@/lib/api";
import { percent } from "@/lib/format";
import { useHydrated } from "@/lib/useHydrated";

type Setting = "digest_enabled" | "alert_emails_enabled";
type Preferences = Record<Setting, boolean>;

const OPTIONS: { setting: Setting; label: string; description: (threshold: string) => string }[] = [
  {
    setting: "alert_emails_enabled",
    label: "Price increases, as they happen",
    description: (threshold) =>
      `An email within minutes when a product's price goes up ${threshold} or more at one of your locations, so you can raise it before the next order.`,
  },
  {
    setting: "digest_enabled",
    label: "Weekly summary",
    description: () =>
      "Every Monday: new price increases, invoices to check, the review queue, and your biggest savings, for each of your locations. Nothing is sent in a quiet week.",
  },
];

/** Which emails you get. Each is saved as soon as it's changed. */
export default function EmailPreferences({ initial, alertThreshold }: { initial: Preferences; alertThreshold: number }) {
  const [prefs, setPrefs] = useState(initial);
  const [saving, setSaving] = useState<Setting | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hydrated = useHydrated();

  async function toggle(setting: Setting, next: boolean) {
    // Shown at once, put back if the save fails: a checkbox that only moves
    // when the server answers looks, for that moment, like it ignored the click.
    setPrefs((p) => ({ ...p, [setting]: next }));
    setSaving(setting);
    setError(null);
    try {
      // keepalive: the box has already moved, so someone may leave the page at
      // once; the save must still land.
      const res = await api("/auth/me", { ...jsonInit("PATCH", { [setting]: next }), keepalive: true });
      if (!res.ok) {
        setPrefs((p) => ({ ...p, [setting]: !next }));
        setError("Couldn't save that. Try again.");
        return;
      }
      const me = await res.json();
      setPrefs({ digest_enabled: me.digest_enabled, alert_emails_enabled: me.alert_emails_enabled });
    } catch {
      setPrefs((p) => ({ ...p, [setting]: !next }));
      setError("Couldn't reach the server. Nothing was changed.");
    } finally {
      setSaving(null);
    }
  }

  return (
    <section className="card max-w-sm p-5">
      <h2 className="section-title">Email</h2>
      <div className="mt-2 space-y-3">
        {OPTIONS.map(({ setting, label, description }) => (
          <label key={setting} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5 h-4 w-4 flex-none accent-brand-600"
              checked={prefs[setting]}
              disabled={!hydrated || saving !== null}
              onChange={(e) => void toggle(setting, e.target.checked)}
            />
            <span>
              {label}
              <span className="block text-gray-500">{description(percent(alertThreshold, { places: 0 }))}</span>
            </span>
          </label>
        ))}
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </section>
  );
}
