"use client";

import { useCallback, useEffect, useState } from "react";

import { useSession } from "@/components/SessionContext";
import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import type { Location } from "@/lib/server";

type User = {
  id: string;
  email: string;
  name: string;
  is_operator: boolean;
  is_active: boolean;
  created_at: string;
  last_sign_in: string | null;
  locations: Location[];
};

type WithPassword = { user: User; generated_password: string | null };

/** A password the server just generated, shown once. */
type Handover = { email: string; password: string; reason: "created" | "reset" };

export default function UsersPage() {
  const session = useSession();
  if (!session?.user.is_operator) {
    return <p className="text-sm text-gray-600">Only operators can manage users.</p>;
  }
  // Operators can open every location, so their list is the full set to grant from.
  return <Users selfId={session.user.id} allLocations={session.user.locations} />;
}

function when(iso: string | null): string {
  if (!iso) return "never";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function Users({ selfId, allLocations }: { selfId: string; allLocations: Location[] }) {
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [handover, setHandover] = useState<Handover | null>(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    const res = await api("/users");
    if (res.ok) setUsers(await res.json());
    else setError("Couldn't load users.");
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  /** Run one change, show the API's reason if it's refused, then reload. */
  async function act(run: () => Promise<Response>, onOk?: (body: unknown) => void) {
    setBusy(true);
    setError(null);
    try {
      const res = await run();
      const body = await res.json().catch(() => null);
      if (!res.ok) {
        setError(formatApiError(body?.detail, "That change didn't go through."));
        return false;
      }
      onOk?.(body);
      await load();
      return true;
    } catch {
      setError("Couldn't reach the server. Nothing was changed.");
      return false;
    } finally {
      setBusy(false);
    }
  }

  function resetPassword(user: User) {
    if (!window.confirm(`Reset ${user.name}'s password? They'll be signed out everywhere.`)) return;
    void act(
      () => api(`/users/${user.id}/password`, jsonInit("POST", {})),
      (body) => {
        const { generated_password } = body as WithPassword;
        if (generated_password) setHandover({ email: user.email, password: generated_password, reason: "reset" });
      },
    );
  }

  function setActive(user: User, active: boolean) {
    if (!active && !window.confirm(`Deactivate ${user.name}? They'll be signed out now and can't sign in.`)) return;
    void act(() => api(`/users/${user.id}`, jsonInit("PATCH", { is_active: active })));
  }

  function setOperator(user: User, operator: boolean) {
    const question = operator
      ? `Make ${user.name} an operator? They'll see every location and can manage users and businesses.`
      : `Remove ${user.name}'s operator access? They'll only see the locations granted to them.`;
    if (!window.confirm(question)) return;
    void act(() => api(`/users/${user.id}`, jsonInit("PATCH", { is_operator: operator })));
  }

  return (
    <div className="max-w-5xl">
      <div className="flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Users</h1>
        {!adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="rounded bg-gray-900 px-3 py-1.5 text-sm font-medium text-white"
          >
            Add user
          </button>
        )}
      </div>
      <p className="mt-1 text-sm text-gray-600">
        Who can sign in, and which locations each person can open. Every change here is recorded in the audit log.
      </p>

      {handover && <PasswordHandover handover={handover} onDone={() => setHandover(null)} />}
      {error && (
        <p role="status" className="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </p>
      )}
      {adding && (
        <AddUser
          allLocations={allLocations}
          busy={busy}
          onCancel={() => setAdding(false)}
          onSubmit={(body) =>
            act(
              () => api("/users", jsonInit("POST", body)),
              (created) => {
                const { user, generated_password } = created as WithPassword;
                if (generated_password) setHandover({ email: user.email, password: generated_password, reason: "created" });
                setAdding(false);
              },
            )
          }
        />
      )}

      {users === null ? (
        <p className="mt-6 text-sm text-gray-500">Loading…</p>
      ) : (
        <table className="mt-6 w-full border-collapse text-sm">
          <thead>
            <tr className="border-b text-left text-gray-500">
              <th className="py-2 pr-4">Person</th>
              <th className="py-2 pr-4">Access</th>
              <th className="py-2 pr-4">Last sign-in</th>
              <th className="py-2" />
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id} className={`border-b align-top ${user.is_active ? "" : "text-gray-400"}`}>
                <td className="py-3 pr-4">
                  <div className="font-medium">
                    {user.name}
                    {user.id === selfId && <span className="ml-1 text-xs font-normal text-gray-400">(you)</span>}
                  </div>
                  <div className="text-gray-500">{user.email}</div>
                  {!user.is_active && <div className="text-xs font-medium uppercase">Deactivated</div>}
                </td>
                <td className="py-3 pr-4">
                  {user.is_operator ? (
                    <span className="text-gray-700">Operator · every location</span>
                  ) : (
                    <LocationAccess
                      user={user}
                      allLocations={allLocations}
                      busy={busy}
                      onGrant={(id) => act(() => api(`/users/${user.id}/locations/${id}`, { method: "PUT" }))}
                      onRevoke={(id) => act(() => api(`/users/${user.id}/locations/${id}`, { method: "DELETE" }))}
                    />
                  )}
                </td>
                <td className="py-3 pr-4 whitespace-nowrap">{when(user.last_sign_in)}</td>
                <td className="py-3 text-right whitespace-nowrap">
                  {user.id !== selfId && (
                    <div className="flex justify-end gap-3 text-xs">
                      {user.is_active && (
                        <button type="button" disabled={busy} onClick={() => resetPassword(user)} className="text-blue-600 hover:underline disabled:opacity-50">
                          Reset password
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setOperator(user, !user.is_operator)}
                        className="text-gray-600 hover:underline disabled:opacity-50"
                      >
                        {user.is_operator ? "Remove operator" : "Make operator"}
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => setActive(user, !user.is_active)}
                        className={`hover:underline disabled:opacity-50 ${user.is_active ? "text-red-700" : "text-gray-600"}`}
                      >
                        {user.is_active ? "Deactivate" : "Reactivate"}
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function LocationAccess({
  user,
  allLocations,
  busy,
  onGrant,
  onRevoke,
}: {
  user: User;
  allLocations: Location[];
  busy: boolean;
  onGrant: (id: string) => void;
  onRevoke: (id: string) => void;
}) {
  const granted = new Set(user.locations.map((l) => l.id));
  const grantable = allLocations.filter((l) => !granted.has(l.id));
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {user.locations.length === 0 && <span className="text-amber-700">No locations: can sign in but sees nothing</span>}
      {user.locations.map((location) => (
        <span key={location.id} className="inline-flex items-center gap-1 rounded bg-gray-100 px-2 py-0.5 text-gray-700">
          {location.name}
          <button
            type="button"
            aria-label={`Remove ${location.name} from ${user.name}`}
            disabled={busy}
            onClick={() => onRevoke(location.id)}
            className="text-gray-400 hover:text-red-700 disabled:opacity-50"
          >
            ×
          </button>
        </span>
      ))}
      {grantable.length > 0 && (
        <select
          aria-label={`Add a location for ${user.name}`}
          disabled={busy}
          value=""
          onChange={(e) => e.target.value && onGrant(e.target.value)}
          className="rounded border border-gray-300 bg-white px-1.5 py-0.5 text-xs text-gray-600"
        >
          <option value="">+ location</option>
          {grantable.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}

function AddUser({
  allLocations,
  busy,
  onCancel,
  onSubmit,
}: {
  allLocations: Location[];
  busy: boolean;
  onCancel: () => void;
  onSubmit: (body: { email: string; name: string; is_operator: boolean; location_ids: string[] }) => void;
}) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [operator, setOperator] = useState(false);
  const [locationIds, setLocationIds] = useState<string[]>([]);

  const input = "w-full rounded border border-gray-300 px-2 py-1.5 text-sm";
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit({ name, email, is_operator: operator, location_ids: operator ? [] : locationIds });
      }}
      className="mt-4 space-y-3 rounded border border-gray-200 bg-white p-4"
    >
      <div className="grid grid-cols-2 gap-3">
        <label className="text-sm">
          <span className="mb-1 block text-gray-600">Name</span>
          <input required value={name} onChange={(e) => setName(e.target.value)} className={input} />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-gray-600">Email</span>
          <input type="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={input} />
        </label>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={operator} onChange={(e) => setOperator(e.target.checked)} />
        Operator: sees every location and can manage users and businesses
      </label>
      {!operator && (
        <fieldset className="text-sm">
          <legend className="mb-1 text-gray-600">Locations</legend>
          <div className="grid max-h-48 grid-cols-2 gap-1 overflow-y-auto rounded border border-gray-200 p-2 sm:grid-cols-3">
            {allLocations.map((l) => (
              <label key={l.id} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={locationIds.includes(l.id)}
                  onChange={(e) =>
                    setLocationIds((ids) => (e.target.checked ? [...ids, l.id] : ids.filter((id) => id !== l.id)))
                  }
                />
                {l.name}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <p className="text-xs text-gray-500">
        A temporary password is generated and shown to you once, to give to them directly.
      </p>
      <div className="flex gap-2">
        <button type="submit" disabled={busy} className="rounded bg-gray-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
          Create login
        </button>
        <button type="button" onClick={onCancel} className="rounded px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100">
          Cancel
        </button>
      </div>
    </form>
  );
}

function PasswordHandover({ handover, onDone }: { handover: Handover; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div role="status" className="mt-4 rounded border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-900">
      <p>
        {handover.reason === "created" ? "Login created for" : "Password reset for"} <strong>{handover.email}</strong>.
        Their temporary password, shown only this once:
      </p>
      <div className="mt-2 flex items-center gap-2">
        <code className="rounded bg-white px-2 py-1 font-mono text-base" data-testid="generated-password">
          {handover.password}
        </code>
        <button
          type="button"
          onClick={() =>
            void navigator.clipboard?.writeText(handover.password).then(
              () => setCopied(true),
              () => setCopied(false),
            )
          }
          className="rounded border border-emerald-300 bg-white px-2 py-1 text-xs"
        >
          {copied ? "Copied" : "Copy"}
        </button>
        <button type="button" onClick={onDone} className="ml-auto text-xs underline">
          Done
        </button>
      </div>
      <p className="mt-2 text-xs">
        Give it to them directly, not by email. They&rsquo;ll choose their own the first time they sign in. It
        can&rsquo;t be shown again; reset it if it&rsquo;s lost.
      </p>
    </div>
  );
}
