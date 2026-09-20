"use client";

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type Basis = "auto" | "peer" | "history";

type NegotiationLine = {
  canonical_sku_id: string;
  canonical_sku_name: string;
  basis: "peer" | "history";
  current_price: string;
  target_price: string;
  peer_account_count: number | null;
  history_observation_count: number | null;
  trailing_quantity: string;
  recoverable_in_window: string;
  annualized_savings: string;
};

type NegotiationSheet = {
  lines: NegotiationLine[];
  window_days: number;
  annualization_factor: string;
  total_annualized_savings: string;
};

const EMPTY: NegotiationSheet = {
  lines: [],
  window_days: 0,
  annualization_factor: "0",
  total_annualized_savings: "0",
};

const BASIS_OPTIONS: { value: Basis; label: string; hint: string }[] = [
  { value: "auto", label: "Best available", hint: "Peer price where we have it, your own history where we don't" },
  { value: "peer", label: "Peer benchmark", hint: "Only SKUs with 5+ independent businesses in the cell" },
  { value: "history", label: "Your own history", hint: "What you used to pay — no peer data needed" },
];

/** The "how do you know that" answer, which is different per basis and is the
 *  first thing a distributor rep asks about any line on this sheet. */
function TargetEvidence({ line }: { line: NegotiationLine }) {
  // Its own line under the price rather than trailing it inline: the history
  // wording is long enough to wrap mid-phrase and shove the row's height
  // around, and this column is the one a rep reads down.
  const text =
    line.basis === "peer"
      ? `peer p25 · ${line.peer_account_count} businesses`
      : `your median · ${line.history_observation_count} priors`;
  return <div className="whitespace-nowrap text-xs text-gray-400">{text}</div>;
}

export default function NegotiationPage() {
  const [basis, setBasis] = useState<Basis>("auto");
  // Held as one object rather than separate pieces of state: every figure on
  // the page (including the total, summed server-side in Decimal per
  // backend/app/api/negotiation.py) belongs to the same sheet, and a partial
  // update would briefly show one basis's lines under another's total.
  const [sheet, setSheet] = useState<NegotiationSheet | null>(null);
  // Distinguished from an empty sheet: "we couldn't ask" and "there is nothing
  // to recover" are very different answers to put in front of someone about to
  // walk into a pricing conversation.
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!TENANT_ID) return;
    let cancelled = false;
    setSheet(null);
    setFailed(false);
    fetch(`${API_BASE}/negotiation?tenant_id=${TENANT_ID}&basis=${basis}`, { cache: "no-store" })
      .then((res) => {
        if (!res.ok) throw new Error(`negotiation request failed: ${res.status}`);
        return res.json();
      })
      .then((data) => {
        // A slower earlier request must not overwrite a newer basis's sheet.
        if (!cancelled) setSheet(data);
      })
      // Without this, a failed request left `sheet` at the null this effect
      // just set and the page stuck on "Loading..." forever, with every
      // further basis click hitting the same dead end.
      .catch(() => {
        if (cancelled) return;
        setSheet(EMPTY);
        setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [basis]);

  if (!TENANT_ID) {
    return (
      <p className="text-sm text-gray-600">
        Set <code>NEXT_PUBLIC_DEV_TENANT_ID</code> in <code>frontend/.env.local</code>.
      </p>
    );
  }

  const emptyMessage = failed
    ? "Couldn't load the sheet — the server didn't respond. Nothing here is out of date; there's just nothing here yet."
    : basis === "peer"
      ? "No overpriced SKUs with enough peer data right now. Try “Your own history” — it needs no peers."
      : "No overpriced SKUs yet. This fills in once a SKU has four deliveries to compare.";

  return (
    <div className="max-w-4xl">
      <div className="mb-4 flex items-center justify-between print:hidden">
        <h1 className="text-xl font-semibold">Negotiation sheet</h1>
        <div className="flex items-center gap-4">
          <div className="flex rounded border border-gray-300 text-sm">
            {BASIS_OPTIONS.map((option) => (
              <button
                key={option.value}
                title={option.hint}
                onClick={() => setBasis(option.value)}
                className={`px-3 py-1.5 first:rounded-l last:rounded-r ${
                  basis === option.value ? "bg-gray-900 text-white" : "bg-white hover:bg-gray-50"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            onClick={() => window.print()}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50"
          >
            Print
          </button>
        </div>
      </div>

      {sheet === null && <p className="text-sm text-gray-500">Loading...</p>}
      {sheet !== null && sheet.lines.length === 0 && <p className="text-sm text-gray-500">{emptyMessage}</p>}
      {sheet !== null && sheet.lines.length > 0 && (
        <>
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b text-left text-gray-500">
                <th className="py-2 pr-4">SKU</th>
                <th className="py-2 pr-4">Current price</th>
                <th className="py-2 pr-4">Target price</th>
                <th className="py-2 pr-4">Qty ({sheet.window_days}d)</th>
                <th className="py-2 pr-4">Recoverable</th>
                <th className="py-2 pr-4">Annualized savings</th>
              </tr>
            </thead>
            <tbody>
              {sheet.lines.map((line) => (
                <tr key={line.canonical_sku_id} className="border-b">
                  <td className="py-2 pr-4">{line.canonical_sku_name}</td>
                  <td className="py-2 pr-4">${line.current_price}</td>
                  <td className="py-2 pr-4">
                    <div>${line.target_price}</div>
                    <TargetEvidence line={line} />
                  </td>
                  <td className="py-2 pr-4">{line.trailing_quantity}</td>
                  <td className="py-2 pr-4">${line.recoverable_in_window}</td>
                  <td className="py-2 pr-4 font-medium">${line.annualized_savings}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-4 text-sm font-medium">
            Total annualized savings opportunity: ${sheet.total_annualized_savings}
          </div>
          {/* Stays on the printed sheet on purpose: every dollar above is a
              projection from this much history, and the rep across the table
              is entitled to know how much that is. */}
          <p className="mt-1 text-xs text-gray-500">
            Recoverable figures cover {sheet.window_days} days of invoices; annualized at{" "}
            {sheet.annualization_factor}&times;.
          </p>
        </>
      )}
    </div>
  );
}
