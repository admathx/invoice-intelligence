"use client";

import { useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api, jsonInit } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";

export type Distributor = { id: string; name: string; slug: string };

const ADD = "__add__";

/** Choosing who an invoice is from: one of this location's distributors, or
 *  a new one added on the spot (a local produce or seafood vendor the shared
 *  list doesn't have; backend app/business_distributors.py).
 *
 *  `stacked` puts the label above the list, for a form; otherwise beside it. */
export default function DistributorPicker({
  distributors,
  value,
  onChange,
  onAdded,
  suggestedName = "",
  addHint,
  disabled = false,
  stacked = false,
}: {
  distributors: Distributor[];
  /** The chosen distributor's id, or "" for none yet. */
  value: string;
  onChange: (id: string) => void;
  /** A distributor was added (or one by that name was already there): the
   *  caller adds it to its list. It is also chosen, through onChange. */
  onAdded: (distributor: Distributor) => void;
  /** What the name box starts with: the name read off the invoice, if any. */
  suggestedName?: string;
  addHint?: string;
  disabled?: boolean;
  stacked?: boolean;
}) {
  const locationId = useLocationId();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState(suggestedName);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add() {
    const typed = name.trim();
    if (!typed || busy || disabled) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(`/distributors?tenant_id=${locationId}`, jsonInit("POST", { name: typed }));
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(formatApiError(data?.detail, "Couldn't add that distributor."));
        return;
      }
      onAdded(data);
      onChange(data.id);
      setAdding(false);
    } catch {
      setError("Couldn't reach the server. Nothing was added.");
    } finally {
      setBusy(false);
    }
  }

  return (
    // `contents`: the list and the name box sit in the caller's own row.
    <div className={stacked ? "min-w-0" : "contents"}>
      <label className={stacked ? "block min-w-0 text-sm" : "flex items-center gap-2"}>
        {stacked ? <span className="mb-1 block text-gray-600">Distributor</span> : "Distributor"}
        <select
          value={value}
          disabled={disabled}
          onChange={(e) => {
            if (e.target.value === ADD) {
              setAdding(true);
              return;
            }
            onChange(e.target.value);
          }}
          className={stacked ? "input w-full" : "input py-1.5"}
        >
          {!value && <option value="">Choose the distributor</option>}
          {distributors.map((d) => (
            <option key={d.id} value={d.id}>
              {d.name}
            </option>
          ))}
          <option value={ADD}>+ Add a distributor…</option>
        </select>
      </label>
      {adding && (
        <span className={`flex flex-wrap items-center gap-2 ${stacked ? "mt-2" : ""}`}>
          <input
            aria-label="New distributor's name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              // Inside a form, Enter here adds the distributor; it doesn't
              // send the form around it.
              if (e.key === "Enter") {
                e.preventDefault();
                void add();
              }
            }}
            placeholder="Their name, as on the invoice"
            className="input py-1.5"
            autoFocus
          />
          <button type="button" onClick={() => void add()} disabled={disabled || busy || !name.trim()} className="btn-secondary btn-sm">
            Add
          </button>
          <button type="button" onClick={() => setAdding(false)} className="text-xs font-medium text-gray-600 hover:underline">
            Cancel
          </button>
          {addHint && <span className="basis-full text-xs text-gray-500">{addHint}</span>}
          {error && (
            <span role="alert" className="basis-full text-xs text-red-700">
              {error}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

/** A list with a distributor just added put in its place by name. */
export function withDistributor(list: Distributor[], added: Distributor): Distributor[] {
  return list.some((d) => d.id === added.id) ? list : [...list, added].sort((a, b) => a.name.localeCompare(b.name));
}
