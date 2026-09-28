"use client";

import { useCallback, useEffect, useState } from "react";

import { useSession } from "@/components/SessionContext";
import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";

type TenantSummary = {
  id: string;
  name: string;
  metro: string;
  volume_tier: string;
  account_id: string | null;
  inbox_address: string | null;
};

// Annual food spend bands (backend app/models/enums.py VolumeTier).
const VOLUME_TIERS: { value: string; label: string }[] = [
  { value: "under_500k", label: "Under $500k" },
  { value: "500k_1m", label: "$500k–$1M" },
  { value: "1m_3m", label: "$1M–$3M" },
  { value: "over_3m", label: "Over $3M" },
];

type Account = {
  id: string;
  name: string;
  created_at: string;
  location_count: number;
  locations?: TenantSummary[];
};

export default function AccountsPage() {
  // The API refuses non-operators anyway (403); this just says so plainly
  // instead of rendering a page of failed requests.
  if (!useSession()?.user.is_operator) {
    return <p className="text-sm text-gray-600">Only operators can manage businesses.</p>;
  }
  return <Businesses />;
}

function Businesses() {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const unassigned = tenants.filter((t) => t.account_id === null);
  const [created, setCreated] = useState<TenantSummary | null>(null);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const list: Account[] = await (await api("/accounts")).json();
      // Each account's locations, so the page shows what a grouping actually
      // contains rather than just a count. Fetched together to avoid a
      // half-loaded page where counts and members disagree.
      const detailed = await Promise.all(
        list.map(async (a) => (await api(`/accounts/${a.id}`)).json())
      );
      setAccounts(detailed);
      setTenants(await (await api("/tenants")).json());
      // No setError(null) here: every action reloads, including a refused
      // one, and clearing here wiped the refusal (a 409 "detach it first",
      // a failed Add location) a moment after it appeared. A successful
      // action clears the error itself (act).
    } catch {
      setAccounts([]);
      setError("Couldn't reach the API.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Runs one change and reloads. Resolves true only if the change was
   *  accepted, so a form can keep what was typed when it wasn't. */
  async function act(run: () => Promise<Response>, onOk?: (body: unknown) => void): Promise<boolean> {
    setBusy(true);
    try {
      const resp = await run();
      // The 409 on re-parenting is the one error a user can actually hit by
      // hand, so it gets shown rather than swallowed into a silent no-op.
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        setError(formatApiError(body?.detail, "Request failed."));
        await load();
        return false;
      }
      setError(null);
      onOk?.(body);
      await load();
      return true;
    } catch {
      setError("Couldn't reach the API.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  const post = (path: string, body: unknown) => api(path, jsonInit("POST", body));

  return (
    <div className="max-w-4xl">
      <h1 className="page-title">Locations and businesses</h1>

      {error && <p className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>}

      <AddLocation
        busy={busy}
        metros={Array.from(new Set(tenants.map((t) => t.metro))).sort()}
        onSubmit={(body) => act(() => post("/tenants", body), (t) => setCreated(t as TenantSummary))}
      />
      {created && (
        <p role="status" className="mt-3 rounded-lg border border-brand-200 border-l-4 border-l-brand-400 bg-brand-50 px-4 py-2.5 text-sm text-brand-950">
          Added <strong>{created.name}</strong>. They forward invoices to{" "}
          <code className="rounded bg-white px-1.5 py-0.5 font-semibold ring-1 ring-brand-200 [overflow-wrap:anywhere]">
            {created.inbox_address}
          </code>
          . Give
          someone access on the Users page.
        </p>
      )}

      <h2 className="section-title mt-8">Businesses</h2>
      <p className="mt-1 max-w-2xl text-sm text-gray-600">
        Group a multi-unit operator&rsquo;s locations into one business. A business counts{" "}
        <strong>once</strong> in a peer benchmark no matter how many locations it has, and its locations
        can&rsquo;t corroborate each other&rsquo;s SKU corrections &mdash; without this, a five-location group
        clears the five-business privacy threshold using nothing but itself.
      </p>

      <form
        className="mt-4 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!newName.trim()) return;
          void act(() => post("/accounts", { name: newName.trim() })).then((ok) => ok && setNewName(""));
        }}
      >
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          placeholder="New business name"
          className="input flex-1"
        />
        <button
          type="submit"
          disabled={busy || !newName.trim()}
          className="btn-primary"
        >
          Create business
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
          <div key={account.id} className="card border-l-4 border-l-brand-300 p-4">
            <div className="flex items-baseline justify-between">
              <h2 className="font-semibold">{account.name}</h2>
              <span className="badge bg-brand-100 text-brand-800">
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
                        api(`/accounts/${account.id}/locations/${location.id}`, { method: "DELETE" })
                      )
                    }
                    className="text-xs font-medium text-red-600 hover:text-red-800 hover:underline disabled:opacity-50"
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
              className="input mt-2 w-full disabled:opacity-50"
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

      <h2 className="section-title mt-8">All locations</h2>
      <div className="card mt-2 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead className="bg-gray-50">
          <tr className="border-b border-gray-200">
            <th className="th pl-4">Location</th>
            <th className="th">Metro</th>
            <th className="th">Forwards invoices to</th>
          </tr>
        </thead>
        <tbody>
          {tenants.map((t) => (
            <tr key={t.id} className="border-b border-gray-100 last:border-0">
              <td className="min-w-[10rem] py-2.5 pl-4 pr-4 font-medium">{t.name}</td>
              <td className="py-2.5 pr-4">
                <span className="badge bg-sky-100 text-sky-800">{t.metro}</span>
              </td>
              <td className="whitespace-nowrap py-2.5 pr-4 font-mono text-xs text-brand-800">{t.inbox_address ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </div>
  );
}

function AddLocation({
  busy,
  metros,
  onSubmit,
}: {
  busy: boolean;
  metros: string[];
  onSubmit: (body: { name: string; metro: string; volume_tier: string }) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [metro, setMetro] = useState("");
  const [tier, setTier] = useState("under_500k");
  const input = "input";
  return (
    <form
      className="card mt-4 border-t-4 border-t-brand-300 p-4"
      onSubmit={(e) => {
        e.preventDefault();
        // Cleared only once the location exists: a refused one keeps what was typed.
        void onSubmit({ name: name.trim(), metro: metro.trim(), volume_tier: tier }).then((ok) => ok && setName(""));
      }}
    >
      <h2 className="section-title">Add a location</h2>
      <p className="mt-0.5 text-xs text-gray-500">
        A new restaurant, or a new site of an existing one. Pick an existing metro where one fits: benchmarks compare
        within a metro, so a second spelling splits it.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <input aria-label="Location name" placeholder="Name" required value={name} onChange={(e) => setName(e.target.value)} className={`${input} flex-1`} />
        <input aria-label="Metro" placeholder="Metro, e.g. Austin, TX" required list="known-metros" value={metro} onChange={(e) => setMetro(e.target.value)} className={input} />
        <datalist id="known-metros">
          {metros.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <select aria-label="Annual food spend" value={tier} onChange={(e) => setTier(e.target.value)} className={input}>
          {VOLUME_TIERS.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
        <button type="submit" disabled={busy || !name.trim() || !metro.trim()} className="btn-primary">
          Add location
        </button>
      </div>
    </form>
  );
}
