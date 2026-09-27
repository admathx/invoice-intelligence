"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import ActivityList from "@/components/ActivityList";
import { useSession } from "@/components/SessionContext";
import type { AuditEvent } from "@/lib/activity";
import { api } from "@/lib/api";

type Page = { events: AuditEvent[]; next_cursor: string | null };
type Person = { id: string; name: string; email: string };

// Families of actions, by prefix (see backend app/api/activity.py audit_log).
const KINDS: { label: string; prefix: string }[] = [
  { label: "Everything", prefix: "" },
  { label: "Invoices", prefix: "invoice." },
  { label: "Review queue", prefix: "invoice_line." },
  { label: "Users and access", prefix: "user." },
  { label: "Sign-ins", prefix: "auth." },
  { label: "Businesses", prefix: "account." },
  { label: "Rejected emails", prefix: "email." },
];

export default function AuditPage() {
  const session = useSession();
  if (!session?.user.is_operator) {
    return <p className="text-sm text-gray-600">Only operators can see the full audit log.</p>;
  }
  return <AuditLog />;
}

function AuditLog() {
  const locations = useSession()?.user.locations ?? [];
  const [people, setPeople] = useState<Person[]>([]);
  const [kind, setKind] = useState("");
  const [actor, setActor] = useState("");
  const [location, setLocation] = useState("");
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  // Bumped whenever the filters change, so an "Older" page still in flight
  // for the previous filters is dropped instead of appended.
  const generation = useRef(0);

  useEffect(() => {
    api("/users")
      .then((res) => (res.ok ? res.json() : []))
      .then(setPeople)
      .catch(() => setPeople([]));
  }, []);

  const fetchPage = useCallback(
    async (after: string | null): Promise<Page | null> => {
      const params = new URLSearchParams({ limit: "100" });
      if (kind) params.set("action", kind);
      if (actor) params.set("actor_id", actor);
      if (location) params.set("tenant_id", location);
      if (after) params.set("cursor", after);
      const res = await api(`/audit?${params}`).catch(() => null);
      return res?.ok ? res.json() : null;
    },
    [kind, actor, location],
  );

  useEffect(() => {
    // A slower response for the previous filters must not replace this one.
    let stale = false;
    generation.current += 1;
    setEvents(null);
    setFailed(false);
    void fetchPage(null).then((page) => {
      if (stale) return;
      setEvents(page?.events ?? []);
      setCursor(page?.next_cursor ?? null);
      setFailed(page === null);
    });
    return () => {
      stale = true;
    };
  }, [fetchPage]);

  async function loadMore() {
    if (!cursor) return;
    const asked = generation.current;
    setLoadingMore(true);
    const page = await fetchPage(cursor);
    setLoadingMore(false);
    if (asked !== generation.current) return;
    if (!page) {
      setFailed(true);
      return;
    }
    setEvents((current) => [...(current ?? []), ...page.events]);
    setCursor(page.next_cursor);
  }

  const select = "rounded border border-gray-300 bg-white px-2 py-1 text-sm";
  return (
    <div className="max-w-4xl">
      <h1 className="text-xl font-semibold">Audit log</h1>
      <p className="mt-1 text-sm text-gray-600">
        Everything, everywhere: including what isn&rsquo;t tied to one location, like sign-ins, logins and businesses.
      </p>

      <div className="mt-4 flex flex-wrap gap-2">
        <select aria-label="What" value={kind} onChange={(e) => setKind(e.target.value)} className={select}>
          {KINDS.map((k) => (
            <option key={k.prefix} value={k.prefix}>
              {k.label}
            </option>
          ))}
        </select>
        <select aria-label="Who" value={actor} onChange={(e) => setActor(e.target.value)} className={select}>
          <option value="">Anyone</option>
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select aria-label="Where" value={location} onChange={(e) => setLocation(e.target.value)} className={select}>
          <option value="">Any location</option>
          {locations.map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
      </div>

      <div className="mt-4">
        {failed && <p className="mb-2 text-sm text-red-700">Couldn&rsquo;t load the log.</p>}
        {events === null ? (
          <p className="text-sm text-gray-500">Loading…</p>
        ) : (
          <ActivityList events={events} linkInvoices showLocation />
        )}
        {cursor && (
          <button
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            className="mt-3 rounded border border-gray-300 bg-white px-3 py-1.5 text-sm disabled:opacity-50"
          >
            {loadingMore ? "Loading…" : "Older"}
          </button>
        )}
      </div>
    </div>
  );
}
