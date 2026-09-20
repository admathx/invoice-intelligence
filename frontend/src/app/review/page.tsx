"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type QueueItem = {
  id: string;
  invoice_id: string;
  invoice_date: string | null;
  distributor_name: string | null;
  raw_description: string;
  raw_sku: string | null;
  raw_pack_size: string | null;
  quantity: string;
  unit_price: string;
  uom: string;
  canonical_sku_id: string | null;
  canonical_sku_name: string | null;
  match_confidence: string | null;
};

type SearchResult = {
  id: string;
  name: string;
  category: string;
  subcategory: string | null;
  base_uom: string;
};

export default function ReviewQueuePage() {
  return (
    <Suspense fallback={<p className="text-sm text-gray-500">Loading review queue...</p>}>
      <ReviewQueueInner />
    </Suspense>
  );
}

function ReviewQueueInner() {
  // Optional ?distributor_id= scopes the queue — useful for a rep working
  // through one distributor at a time, and for e2e tests that need a clean
  // slice of the queue instead of every pending line across every source.
  const distributorId = useSearchParams().get("distributor_id");
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [index, setIndex] = useState(0);
  const [clearedCount, setClearedCount] = useState(0);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [selected, setSelected] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startedAt] = useState(() => Date.now());
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!TENANT_ID) return;
    // Ignore a stale response: if distributorId changes again before this
    // request resolves, an out-of-order response must not clobber the
    // queue for whichever filter is current by the time it lands.
    let ignore = false;
    const params = new URLSearchParams({ tenant_id: TENANT_ID });
    if (distributorId) params.set("distributor_id", distributorId);
    fetch(`${API_BASE}/review/queue?${params}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (ignore) return;
        setQueue(data);
        // Reset the per-item form state here too, not just via the [index]
        // effect: if the user is already sitting at index 0, setIndex(0) is
        // a no-op, that effect never re-fires, and a stale search dropdown
        // from the *previous* queue's item stays on screen — one Enter away
        // from applying a correction to a completely different line item.
        setIndex(0);
        setQuery("");
        setResults([]);
        setSelected(0);
        setError(null);
      });
    return () => {
      ignore = true;
    };
  }, [distributorId]);

  const current = queue?.[index] ?? null;

  useEffect(() => {
    setQuery("");
    setResults([]);
    setSelected(0);
    setError(null);
    inputRef.current?.focus();
  }, [index]);

  async function runSearch(q: string) {
    setQuery(q);
    setSelected(0);
    if (!q.trim()) {
      setResults([]);
      return;
    }
    const res = await fetch(`${API_BASE}/skus?q=${encodeURIComponent(q)}`);
    setResults(res.ok ? await res.json() : []);
  }

  function advance() {
    setClearedCount((c) => c + 1);
    setIndex((i) => i + 1);
  }

  async function confirm() {
    if (!current || !current.canonical_sku_id || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/review/${current.id}/confirm?tenant_id=${TENANT_ID}`, {
        method: "POST",
      });
      if (!res.ok) {
        setError((await res.json()).detail ?? "confirm failed");
        return;
      }
      advance();
    } finally {
      setBusy(false);
    }
  }

  async function correct(skuId: string) {
    if (!current || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/review/${current.id}/correct?tenant_id=${TENANT_ID}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ canonical_sku_id: skuId }),
      });
      if (!res.ok) {
        setError((await res.json()).detail ?? "correct failed");
        return;
      }
      advance();
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSelected((s) => Math.min(s + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSelected((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (results.length > 0) {
        // Clamped rather than trusting `selected` to already be in range:
        // it's reset to 0 in a few places but nothing enforces that pairing
        // structurally, so a future producer of `results` that forgets to
        // reset it should degrade to "pick the last item," not throw.
        const target = results[Math.min(selected, results.length - 1)];
        correct(target.id);
      } else if (!query.trim() && current?.canonical_sku_id) {
        confirm();
      }
    } else if (e.key === "Escape") {
      setQuery("");
      setResults([]);
      setSelected(0);
    }
  }

  if (!TENANT_ID) {
    return (
      <p className="text-sm text-gray-600">
        Set <code>NEXT_PUBLIC_DEV_TENANT_ID</code> in <code>frontend/.env.local</code>.
      </p>
    );
  }

  if (queue === null) {
    return <p className="text-sm text-gray-500">Loading review queue...</p>;
  }

  if (!current) {
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    return (
      <div>
        <h1 className="mb-2 text-xl font-semibold">Review queue</h1>
        <p className="text-sm text-gray-600">
          {clearedCount > 0
            ? `Cleared ${clearedCount} item${clearedCount === 1 ? "" : "s"} in ${seconds}s. Queue is empty.`
            : "Queue is empty — nothing needs review."}
        </p>
      </div>
    );
  }

  return (
    <div className="max-w-2xl">
      <div className="mb-4 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Review queue</h1>
        <span className="text-sm text-gray-500">
          {index + 1} of {queue.length} &middot; {clearedCount} cleared
        </span>
      </div>

      <div className="rounded border border-gray-200 bg-white p-4">
        <div className="mb-1 text-sm text-gray-500">
          {current.distributor_name ?? "Unknown distributor"} &middot; {current.invoice_date ?? "—"}
        </div>
        <div className="mb-2 text-lg font-medium">{current.raw_description}</div>
        <div className="mb-3 text-sm text-gray-600">
          SKU {current.raw_sku ?? "—"} &middot; pack {current.raw_pack_size ?? "—"} &middot; qty{" "}
          {current.quantity} {current.uom} &middot; ${current.unit_price}
        </div>

        {current.canonical_sku_id ? (
          <div className="mb-3 rounded bg-blue-50 px-3 py-2 text-sm">
            Suggested match: <strong>{current.canonical_sku_name}</strong>{" "}
            {current.match_confidence && (
              <span className="text-gray-500">({Number(current.match_confidence).toFixed(2)})</span>
            )}
            . Press <kbd className="rounded border bg-white px-1">Enter</kbd> to confirm, or search below to
            correct.
          </div>
        ) : (
          <div className="mb-3 rounded bg-amber-50 px-3 py-2 text-sm">
            No confident match — search below to assign the correct canonical SKU.
          </div>
        )}

        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => runSearch(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          placeholder="Search canonical SKU to correct..."
          className="w-full rounded border border-gray-300 px-3 py-2 text-sm"
          autoFocus
        />
        {results.length > 0 && (
          <ul className="mt-2 divide-y divide-gray-100 rounded border border-gray-200">
            {results.map((r, i) => (
              <li
                key={r.id}
                className={`cursor-pointer px-3 py-2 text-sm ${i === selected ? "bg-blue-100" : ""}`}
                onMouseEnter={() => setSelected(i)}
                onClick={() => correct(r.id)}
              >
                {r.name}{" "}
                <span className="text-gray-500">
                  {r.category}
                  {r.subcategory ? ` / ${r.subcategory}` : ""} · {r.base_uom}
                </span>
              </li>
            ))}
          </ul>
        )}
        {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      </div>
    </div>
  );
}
