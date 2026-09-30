"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import NoLocation from "@/components/NoLocation";
import { useLocationId } from "@/components/SessionContext";
import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { quantity, unitPrice } from "@/lib/format";

/** How sure the matcher was: suggestions start at 60% (validation/thresholds.yaml). */
function confidenceBadge(confidence: number): string {
  if (confidence >= 0.9) return "bg-brand-100 text-brand-800";
  if (confidence >= 0.85) return "bg-amber-100 text-amber-800";
  return "bg-red-100 text-red-700";
}

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
  price_known: boolean;
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
    <Suspense fallback={<p className="text-sm text-gray-500">Loading…</p>}>
      <ReviewQueueInner />
    </Suspense>
  );
}

function ReviewQueueInner() {
  // Optional ?distributor_id= scopes the queue — useful for a rep working
  // through one distributor at a time, and for e2e tests that need a clean
  // slice of the queue instead of every pending line across every source.
  const distributorId = useSearchParams().get("distributor_id");
  const locationId = useLocationId();
  const [queue, setQueue] = useState<QueueItem[] | null>(null);
  const [index, setIndex] = useState(0);
  const [clearedCount, setClearedCount] = useState(0);
  const [skippedCount, setSkippedCount] = useState(0);
  const [loadFailed, setLoadFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  // What the results on screen are for: "no matches" only once a search
  // has actually answered, not while it's still on its way.
  const [answered, setAnswered] = useState("");
  const [selected, setSelected] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [startedAt] = useState(() => Date.now());
  const inputRef = useRef<HTMLInputElement>(null);
  const latestSearch = useRef(0);

  useEffect(() => {
    if (!locationId) return;
    // Ignore a stale response: if distributorId changes again before this
    // request resolves, an out-of-order response must not clobber the
    // queue for whichever filter is current by the time it lands.
    let ignore = false;
    const params = new URLSearchParams({ tenant_id: locationId });
    if (distributorId) params.set("distributor_id", distributorId);
    setLoadFailed(false);
    api(`/review/queue?${params}`)
      .then((res) => {
        if (!res.ok) throw new Error(`queue: ${res.status}`);
        return res.json();
      })
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
      })
      // Not "nothing to match": an error that looked like finished work sent
      // people away with items still waiting.
      .catch(() => {
        if (!ignore) setLoadFailed(true);
      });
    return () => {
      ignore = true;
    };
  }, [distributorId, locationId, attempt]);

  const current = queue?.[index] ?? null;

  useEffect(() => {
    // Also retires any search still in flight for the previous item, so its
    // results can't appear under this one.
    latestSearch.current += 1;
    setQuery("");
    setResults([]);
    setSelected(0);
    setError(null);
    inputRef.current?.focus();
  }, [index]);

  async function runSearch(q: string) {
    // Every keystroke fires a search, and responses can land out of order.
    // Only the newest request may set `results`: a late response for "mo"
    // replacing the one for "mozz" put Mops at the top of the list, one Enter
    // away from correcting the line to the wrong SKU (and writing an alias
    // and a price observation for it).
    const requestId = ++latestSearch.current;
    setQuery(q);
    setSelected(0);
    if (!q.trim()) {
      setResults([]);
      return;
    }
    try {
      const res = await api(`/skus?q=${encodeURIComponent(q)}`);
      const data = res.ok ? await res.json() : [];
      if (requestId === latestSearch.current) {
        setResults(data);
        setAnswered(q);
      }
    } catch {
      if (requestId === latestSearch.current) setResults([]);
    }
  }

  function advance() {
    setClearedCount((c) => c + 1);
    setIndex((i) => i + 1);
  }

  /** Leave this one for later: it stays waiting, and comes back next time. */
  function skip() {
    setSkippedCount((c) => c + 1);
    setIndex((i) => i + 1);
  }

  async function confirm() {
    if (!current || !current.canonical_sku_id || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(`/review/${current.id}/confirm?tenant_id=${locationId}`, { method: "POST" });
      if (!res.ok) {
        setError(formatApiError((await res.json().catch(() => null))?.detail, "Couldn't save that. Try again."));
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
      const res = await api(
        `/review/${current.id}/correct?tenant_id=${locationId}`,
        jsonInit("POST", { canonical_sku_id: skuId }),
      );
      if (!res.ok) {
        setError(formatApiError((await res.json().catch(() => null))?.detail, "Couldn't save that. Try again."));
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

  if (!locationId) return <NoLocation />;

  if (loadFailed) {
    return (
      <div className="max-w-2xl">
        <PageHeading />
        <div role="alert" className="card border-l-4 border-l-red-400 px-4 py-4 text-sm text-gray-700">
          Couldn&rsquo;t load the items to match.
          <button type="button" className="btn-secondary btn-sm ml-3" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </button>
        </div>
      </div>
    );
  }

  if (queue === null) {
    return <p className="text-sm text-gray-500">Loading…</p>;
  }

  if (!current) {
    const seconds = Math.round((Date.now() - startedAt) / 1000);
    return (
      <div className="max-w-2xl">
        <PageHeading />
        <div className="card border-l-4 border-l-brand-400 px-4 py-4 text-sm text-gray-700">
          <div className="flex items-center gap-3">
            <span aria-hidden className="grid h-8 w-8 flex-none place-items-center rounded-full bg-brand-100 text-brand-800">
              ✓
            </span>
            <span>
              {clearedCount > 0
                ? `Done. You matched ${clearedCount} item${clearedCount === 1 ? "" : "s"} in ${seconds} seconds.`
                : "Nothing to match right now."}
              {skippedCount > 0 &&
                ` ${skippedCount} skipped item${skippedCount === 1 ? " is" : "s are"} still waiting for next time.`}
            </span>
          </div>
          <p className="mt-3 flex flex-wrap gap-2">
            <a href="/insights" className="btn-secondary btn-sm">
              See price alerts
            </a>
            <a href="/negotiation" className="btn-secondary btn-sm">
              See savings
            </a>
          </p>
        </div>
      </div>
    );
  }

  const confidence = current.match_confidence === null ? null : Number(current.match_confidence);
  const progress = queue.length ? (index / queue.length) * 100 : 0;

  return (
    <div className="max-w-2xl">
      <div className="mb-2 flex flex-wrap items-end justify-between gap-x-4">
        <PageHeading />
        <span className="num mb-3 text-sm text-gray-500">
          <strong className="text-gray-900">{index + 1}</strong> of {queue.length} &middot;{" "}
          <span className="font-semibold text-brand-700">{clearedCount} done</span>
        </span>
      </div>
      <div className="mb-4 h-1.5 overflow-hidden rounded-full bg-gray-200" aria-hidden>
        <div className="h-full rounded-full bg-brand-400 transition-all" style={{ width: `${progress}%` }} />
      </div>

      <div className="card p-5" data-testid="review-item">
        <div className="mb-1 flex flex-wrap justify-between gap-x-3 text-sm text-gray-500">
          <span>
            {current.distributor_name ?? "Distributor not known"} &middot; {current.invoice_date ?? "No date"}
          </span>
          <a href={`/invoices/${current.invoice_id}`} className="link">
            See the invoice
          </a>
        </div>
        <div className="mb-2 text-lg font-semibold" data-testid="review-description">
          {current.raw_description}
        </div>
        <div className="num mb-4 flex flex-wrap gap-x-4 gap-y-1 text-sm text-gray-600">
          <span>Item code {current.raw_sku ?? "—"}</span>
          <span>Pack {current.raw_pack_size ?? "—"}</span>
          <span>
            Qty {quantity(current.quantity)} {current.uom}
          </span>
          <span className="font-semibold text-gray-900">{unitPrice(current.unit_price)}</span>
        </div>

        {!current.price_known && (
          <p className="mb-3 text-sm text-gray-600" data-testid="price-unknown">
            Its price can&rsquo;t be tracked yet:{" "}
            {current.uom === "CS" ? (
              <>
                we can&rsquo;t work out a price per unit from its pack size (
                {current.raw_pack_size ?? "none printed"}). If that was misread, correct it on{" "}
                <a href={`/invoices/${current.invoice_id}`} className="link">
                  the invoice
                </a>
                .
              </>
            ) : (
              <>
                a pack of {current.raw_pack_size ?? "unknown size"} billed per {current.uom} doesn&rsquo;t say what the
                price covers. If it&rsquo;s for the whole case, set the unit to CS on{" "}
                <a href={`/invoices/${current.invoice_id}`} className="link">
                  the invoice
                </a>
                .
              </>
            )}
          </p>
        )}

        {current.canonical_sku_id ? (
          <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-sky-200 bg-sky-50 px-3 py-2.5 text-sm text-sky-950">
            <span>
              Looks like: <strong>{current.canonical_sku_name}</strong>
            </span>
            {confidence !== null && (
              <span className={`badge num ${confidenceBadge(confidence)}`}>{Math.round(confidence * 100)}% sure</span>
            )}
            <span className="basis-full text-xs text-sky-800">
              Press <kbd className="rounded border border-sky-300 bg-white px-1">Enter</kbd> if that&rsquo;s right. If
              not, search for the right product below.
            </span>
            <button type="button" onClick={() => void confirm()} disabled={busy} className="btn-primary btn-sm">
              ✓ That&rsquo;s right
            </button>
          </div>
        ) : (
          <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
            We couldn&rsquo;t tell which product this is. Search for it below.
          </div>
        )}

        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => runSearch(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={busy}
          aria-label="Search products"
          placeholder="Search products, e.g. mozzarella"
          className="input w-full"
          autoFocus
        />
        {results.length > 0 && (
          <ul className="mt-2 divide-y divide-gray-100 overflow-hidden rounded-lg border border-gray-200">
            {results.map((r, i) => (
              <li
                key={r.id}
                className={`cursor-pointer px-3 py-2 text-sm ${i === selected ? "bg-brand-100" : "hover:bg-gray-50"}`}
                onMouseEnter={() => setSelected(i)}
                onClick={() => correct(r.id)}
              >
                <span className="font-medium">{r.name}</span>{" "}
                <span className="text-gray-500">
                  {r.category}
                  {r.subcategory ? ` / ${r.subcategory}` : ""} · priced per {r.base_uom}
                </span>
              </li>
            ))}
          </ul>
        )}
        {query.trim() && answered === query && results.length === 0 && (
          <p className="mt-2 text-sm text-gray-500">No products match &ldquo;{query.trim()}&rdquo;. Try a shorter word.</p>
        )}
        {error && (
          <p className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
        )}
        <div className="mt-4 flex justify-end">
          <button type="button" onClick={skip} disabled={busy} className="btn-secondary btn-sm">
            Skip for now
          </button>
        </div>
      </div>
    </div>
  );
}

function PageHeading() {
  return (
    <div className="mb-3">
      <h1 className="page-title">Match items</h1>
      <p className="mt-0.5 text-sm text-gray-500">
        Say which product each invoice item is, so its price can be tracked.
      </p>
    </div>
  );
}
