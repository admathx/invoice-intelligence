"use client";

import { useEffect, useState } from "react";

import NoLocation from "@/components/NoLocation";
import { useLocationId, useSession } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { money, quantity, unitPrice } from "@/lib/format";

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
  { value: "auto", label: "Best available", hint: "What other businesses pay where we know it, otherwise what you used to pay" },
  { value: "peer", label: "What others pay", hint: "Only products that at least 5 other businesses buy" },
  { value: "history", label: "What you used to pay", hint: "Your own past prices, so it works for every product" },
];

/** The "how do you know that" answer, which is different per basis and is the
 *  first thing a distributor rep asks about any line on this sheet. */
function TargetEvidence({ line }: { line: NegotiationLine }) {
  // Its own line under the price rather than trailing it inline: the history
  // wording is long enough to wrap mid-phrase and shove the row's height
  // around, and this column is the one a rep reads down.
  const text =
    line.basis === "peer"
      ? `a low price among ${line.peer_account_count} businesses`
      : `your usual price, from ${line.history_observation_count} deliveries`;
  return <div className="whitespace-nowrap text-xs text-gray-400">{text}</div>;
}

export default function NegotiationPage() {
  const [basis, setBasis] = useState<Basis>("auto");
  const locationId = useLocationId();
  const locationName = useSession()?.user.locations.find((l) => l.id === locationId)?.name;
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
    if (!locationId) return;
    let cancelled = false;
    setSheet(null);
    setFailed(false);
    api(`/negotiation?tenant_id=${locationId}&basis=${basis}`)
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
  }, [basis, locationId]);

  if (!locationId) return <NoLocation />;

  const emptyMessage = failed
    ? "Couldn't load your savings. Try again in a moment."
    : basis === "peer"
      ? "Nothing to save on yet: not enough other businesses buy the same products. Try “What you used to pay”."
      : "Nothing to save on yet. This fills in once a product has been delivered four times.";

  return (
    <div className="max-w-5xl">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3 print:hidden">
        <div className="min-w-0">
          <h1 className="page-title">Savings</h1>
          <p className="mt-0.5 text-sm text-gray-500">
            Products you could be paying less for. Print this and take it to your rep.
          </p>
        </div>
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-3">
          <div className="flex max-w-full overflow-x-auto rounded-lg bg-gray-100 p-1 text-sm">
            {BASIS_OPTIONS.map((option) => (
              <button
                key={option.value}
                title={option.hint}
                onClick={() => setBasis(option.value)}
                aria-pressed={basis === option.value}
                className={`whitespace-nowrap rounded-md px-3 py-1.5 font-medium transition-colors ${
                  basis === option.value ? "bg-white text-brand-800 shadow-sm ring-1 ring-brand-300" : "text-gray-600 hover:text-gray-900"
                }`}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button onClick={() => window.print()} className="btn-primary">
            Print
          </button>
        </div>
      </div>

      {/* The screen's heading row is hidden on paper; the sheet still says
          what it is and whose it is. */}
      <h1 className="mb-3 hidden text-xl font-semibold print:block">
        Prices to review{locationName ? ` · ${locationName}` : ""}
      </h1>
      {sheet === null && <p className="text-sm text-gray-500">Loading…</p>}
      {sheet !== null && sheet.lines.length === 0 && (
        <div className="card px-4 py-4 text-sm text-gray-600">{emptyMessage}</div>
      )}
      {sheet !== null && sheet.lines.length > 0 && (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-brand-200 bg-gradient-to-r from-brand-50 to-white px-5 py-4">
            <div>
              <div className="text-xs font-semibold uppercase tracking-wide text-brand-800">Possible savings</div>
              <div className="num text-3xl font-bold text-brand-700">{money(sheet.total_annualized_savings)}</div>
              <div className="text-xs text-gray-500">a year, if these prices came down to the target</div>
            </div>
            <div className="num text-right text-sm text-gray-600">
              <strong className="text-gray-900">{sheet.lines.length}</strong> product{sheet.lines.length === 1 ? "" : "s"} to raise with your rep
            </div>
          </div>
          <div className="card overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead className="bg-gray-50">
                <tr className="border-b border-gray-200">
                  <th className="th pl-4">Product</th>
                  <th className="th text-right">You pay</th>
                  <th className="th text-right">Target</th>
                  <th className="th text-right">Bought ({sheet.window_days} days)</th>
                  <th className="th text-right">Saving ({sheet.window_days} days)</th>
                  <th className="th text-right">Saving per year</th>
                </tr>
              </thead>
              <tbody>
                {sheet.lines.map((line) => (
                  <tr key={line.canonical_sku_id} className="border-b border-gray-100 last:border-0">
                    <td className="min-w-[10rem] py-2.5 pl-4 pr-4 font-medium">
                      <a href={`/skus/${line.canonical_sku_id}`} className="hover:text-brand-700 hover:underline print:no-underline">
                        {line.canonical_sku_name}
                      </a>
                    </td>
                    <td className="num whitespace-nowrap py-2.5 pr-4 text-right font-medium text-red-600">
                      {unitPrice(line.current_price)}
                    </td>
                    <td className="whitespace-nowrap py-2.5 pr-4 text-right">
                      <div className="num font-medium text-brand-700">{unitPrice(line.target_price)}</div>
                      <TargetEvidence line={line} />
                    </td>
                    <td className="num whitespace-nowrap py-2.5 pr-4 text-right text-gray-600">{quantity(line.trailing_quantity)}</td>
                    <td className="num whitespace-nowrap py-2.5 pr-4 text-right text-brand-700">
                      {money(line.recoverable_in_window)}
                    </td>
                    <td className="num py-2.5 pr-4 text-right">
                      <span className="badge bg-brand-100 text-sm text-brand-800">{money(line.annualized_savings)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {/* Stays on the printed sheet on purpose: every dollar above is a
              projection from this much history, and the rep across the table
              is entitled to know how much that is. */}
          <p className="mt-2 text-xs text-gray-500">
            Based on the last {sheet.window_days} days of invoices, scaled up to a year (&times;
            {sheet.annualization_factor}).
          </p>
        </>
      )}
    </div>
  );
}
