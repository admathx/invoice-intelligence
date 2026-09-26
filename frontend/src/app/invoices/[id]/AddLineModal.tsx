"use client";

import { useEffect, useRef, useState } from "react";

import type { InvoiceDetail } from "./InvoiceReview";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type Suggestion = {
  raw_description: string;
  raw_sku: string | null;
  raw_pack_size: string | null;
  uom: string;
  canonical_sku_name: string | null;
  last_unit_price: string;
  last_seen: string;
};

type Fields = {
  raw_description: string;
  raw_sku: string;
  raw_pack_size: string;
  uom: string;
  quantity: string;
  unit_price: string;
  extended_price: string;
};

const EMPTY: Fields = {
  raw_description: "",
  raw_sku: "",
  raw_pack_size: "",
  uom: "CS",
  quantity: "",
  unit_price: "",
  extended_price: "",
};

/** Enter a line read off the invoice image, optionally starting from something
 *  this tenant has bought before.
 *
 *  Picking a past purchase fills in what identifies the item (description,
 *  item code, pack size, unit) but deliberately NOT the price. The last price
 *  is shown next to an empty field as a hint only: prefilling it would let an
 *  untouched field record "no increase", erasing exactly the creep this
 *  product exists to catch. Nothing here computes the extended price either;
 *  every printed number is typed, so the server's arithmetic check can catch
 *  a typo instead of agreeing with a value derived from it.
 */
export default function AddLineModal({
  invoiceId,
  onClose,
  onAdded,
}: {
  invoiceId: string;
  onClose: () => void;
  onAdded: (invoice: InvoiceDetail) => void;
}) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const [picked, setPicked] = useState<Suggestion | null>(null);
  const [fields, setFields] = useState<Fields>(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latestSearch = useRef(0);
  const searchRef = useRef<HTMLInputElement>(null);
  const quantityRef = useRef<HTMLInputElement>(null);

  // Same stale-response guard as the review queue's search: a late reply for
  // an earlier keystroke must not replace the list for the current one.
  async function search(q: string) {
    const requestId = ++latestSearch.current;
    setQuery(q);
    try {
      const params = new URLSearchParams({ tenant_id: TENANT_ID, q });
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}/line-item-suggestions?${params}`, { cache: "no-store" });
      const data = res.ok ? await res.json() : [];
      if (requestId === latestSearch.current) setSuggestions(data);
    } catch {
      if (requestId === latestSearch.current) setSuggestions([]);
    }
  }

  useEffect(() => {
    void search(""); // recent purchases, before anything is typed
    searchRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function pick(s: Suggestion) {
    setPicked(s);
    setFields((f) => ({
      ...f,
      raw_description: s.raw_description,
      raw_sku: s.raw_sku ?? "",
      raw_pack_size: s.raw_pack_size ?? "",
      uom: s.uom,
    }));
    setSuggestions([]);
    setQuery("");
    quantityRef.current?.focus();
  }

  const set = (name: keyof Fields) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setFields((f) => ({ ...f, [name]: e.target.value }));

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoiceId}/line-items?tenant_id=${TENANT_ID}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...fields,
          raw_sku: fields.raw_sku || null,
          raw_pack_size: fields.raw_pack_size || null,
        }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string"
            ? detail
            : Array.isArray(detail)
              ? detail.map((d: { loc: string[]; msg: string }) => `${d.loc.at(-1)}: ${d.msg}`).join("; ")
              : "Couldn't add the line."
        );
        return;
      }
      onAdded(data);
    } catch {
      setError("Couldn't reach the server. Nothing was added.");
    } finally {
      setBusy(false);
    }
  }

  const input = "w-full rounded border border-gray-300 px-2 py-1.5 text-sm";
  const complete =
    fields.raw_description.trim() && fields.uom.trim() && fields.quantity.trim() && fields.unit_price.trim() && fields.extended_price.trim();

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-16" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-line-title"
        className="w-full max-w-lg rounded-lg border border-gray-200 bg-white p-5 shadow-lg"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 id="add-line-title" className="text-base font-semibold">
          Add line item
        </h2>
        <p className="mt-1 text-xs text-gray-500">Enter the line as it&rsquo;s printed on the invoice.</p>

        <div className="relative mt-4">
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => void search(e.target.value)}
            placeholder="Start from something you've bought before…"
            aria-label="Search past purchases"
            className={input}
          />
          {suggestions.length > 0 && (
            <ul className="absolute z-10 mt-1 max-h-64 w-full overflow-auto rounded border border-gray-200 bg-white shadow">
              {suggestions.map((s) => (
                <li key={`${s.raw_sku}|${s.raw_description}|${s.raw_pack_size}|${s.uom}`}>
                  <button
                    type="button"
                    onClick={() => pick(s)}
                    className="block w-full px-3 py-2 text-left text-sm hover:bg-blue-50"
                  >
                    <span className="font-medium">{s.raw_description}</span>
                    <span className="block text-xs text-gray-500">
                      {s.raw_sku ?? "no item code"} · {s.raw_pack_size ?? "no pack size"} · {s.uom}
                      {s.canonical_sku_name ? ` · ${s.canonical_sku_name}` : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        <form onSubmit={save} className="mt-4 grid grid-cols-2 gap-3">
          <label className="col-span-2 text-xs text-gray-600">
            Description
            <input value={fields.raw_description} onChange={set("raw_description")} className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Item code
            <input value={fields.raw_sku} onChange={set("raw_sku")} className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Pack size
            <input value={fields.raw_pack_size} onChange={set("raw_pack_size")} placeholder="e.g. 4/5 LB" className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Quantity
            <input ref={quantityRef} inputMode="decimal" value={fields.quantity} onChange={set("quantity")} className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Unit (UOM)
            <input value={fields.uom} onChange={set("uom")} className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Unit price
            <input inputMode="decimal" value={fields.unit_price} onChange={set("unit_price")} className={input} />
          </label>
          <label className="text-xs text-gray-600">
            Extended price
            <input inputMode="decimal" value={fields.extended_price} onChange={set("extended_price")} className={input} />
          </label>

          {picked && (
            <p className="col-span-2 rounded bg-gray-50 px-3 py-2 text-xs text-gray-600">
              Last paid <strong>${picked.last_unit_price}</strong> per {picked.uom} on {picked.last_seen}. Type the
              price this invoice shows, even if it&rsquo;s the same.
            </p>
          )}
          {error && <p className="col-span-2 text-sm text-red-700">{error}</p>}

          <div className="col-span-2 mt-1 flex justify-end gap-2">
            <button type="button" onClick={onClose} className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50">
              Cancel
            </button>
            <button
              type="submit"
              disabled={busy || !complete}
              className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white hover:bg-gray-800 disabled:opacity-40"
            >
              Add line
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
