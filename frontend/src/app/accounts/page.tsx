"use client";

import { useCallback, useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type TenantSummary = {
  id: string;
  name: string;
  metro: string;
  volume_tier: string;
  account_id: string | null;
};

type Account = {
  id: string;
  name: string;
  created_at: string;
  location_count: number;
  locations?: TenantSummary[];
};

export default function AccountsPage() {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [unassigned, setUnassigned] = useState<TenantSummary[]>([]);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const list: Account[] = await (await fetch(`${API_BASE}/accounts`, { cache: "no-store" })).json();
      // Each account's locations, so the page shows what a grouping actually
      // contains rather than just a count. Fetched together to avoid a
      // half-loaded page where counts and members disagree.
      const detailed = await Promise.all(
        list.map(async (a) => (await fetch(`${API_BASE}/accounts/${a.id}`, { cache: "no-store" })).json())
      );
      setAccounts(detailed);
      setUnassigned(await (await fetch(`${API_BASE}/tenants?unassigned=true`, { cache: "no-store" })).json());
      setError(null);
    } catch {
      setAccounts([]);
      setError("Couldn't reach the API.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(run: () => Promise<Response>) {
    setBusy(true);
    try {
      const resp = await run();
      // The 409 on re-parenting is the one error a user can actually hit by
      // hand, so it gets shown rather than swallowed into a silent no-op.
      if (!resp.ok) setError(((await resp.json().catch(() => null)) as { detail?: string } | null)?.detail ?? "Request failed.");
      else setError(null);
      await load();
    } catch {
      setError("Couldn't reach the API.");
    } finally {
      setBusy(false);
    }
  }

  const post = (path: string, body: unknown) =>
    fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  return (
    <div className="max-w-3xl">
      <h1 className="text-xl font-semibold">Businesses</h1>
      <p className="mt-1 max-w-2xl text-sm text-gray-600">
        Group a multi-unit operator&rsquo;s locations into one business. A business counts{" "}
        <strong>once</strong> in a peer benchmark no matter how many locations it has, and its locations
        can&rsquo;t corroborate each other&rsquo;s SKU corrections &mdash; without this, a five-location group
        clears the five-business privacy threshold using nothing but itself.
      </p>

      {error && <p className="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      <form
        className="mt-4 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newName.trim()) return;
          void act(() => post("/accounts", { name: newName.trim() })).then(() => setNewName(""));
        }}
      >
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="New business name"
          className="flex-1 rounded border border-gray-300 px-3 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={busy || !newName.trim()}
          className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50"
        >
          Create
        </button>
      </form>

      {accounts === null && <p className="mt-4 text-sm text-gray-500">Loading...</p>}
      {accounts !== null && accounts.length === 0 && !error && (
        <p className="mt-4 text-sm text-gray-500">
          No businesses yet. Every tenant currently counts as its own, which is correct for single-location
          customers.
        </p>
      )}

      <div className="mt-4 space-y-3">
        {accounts?.map((account) => (
          <div key={account.id} className="rounded border border-gray-200 bg-white p-4">
            <div className="flex items-baseline justify-between">
              <h2 className="font-medium">{account.name}</h2>
              <span className="text-xs text-gray-500">
                {account.location_count} location{account.location_count === 1 ? "" : "s"} &middot; 1 vote in any
                benchmark
              </span>
            </div>

            <ul className="mt-2 divide-y divide-gray-100 text-sm">
              {account.locations?.map((location) => (
                <li key={location.id} className="flex items-center justify-between py-1.5">
                  <span>
                    {location.name} <span className="text-xs text-gray-400">{location.metro}</span>
                  </span>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void act(() =>
                        fetch(`${API_BASE}/accounts/${account.id}/locations/${location.id}`, { method: "DELETE" })
                      )
                    }
                    className="text-xs text-gray-500 hover:text-red-700 disabled:opacity-50"
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>

            <select
              disabled={busy || unassigned.length === 0}
              value=""
              onChange={(e) =>
                e.target.value && void act(() => post(`/accounts/${account.id}/locations`, { tenant_id: e.target.value }))
              }
              className="mt-2 w-full rounded border border-gray-300 px-2 py-1.5 text-sm disabled:opacity-50"
            >
              <option value="">
                {unassigned.length === 0 ? "No unassigned locations" : "Add a location..."}
              </option>
              {unassigned.map((tenant) => (
                <option key={tenant.id} value={tenant.id}>
                  {tenant.name} ({tenant.metro})
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>
    </div>
  );
}
