"use client";

import { useEffect, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type NegotiationLine = {
  canonical_sku_id: string;
  canonical_sku_name: string;
  current_price: string;
  target_price: string;
  peer_distinct_tenant_count: number;
  trailing_90d_quantity: string;
  recoverable_90d: string;
  annualized_savings: string;
};

export default function NegotiationPage() {
  const [lines, setLines] = useState<NegotiationLine[] | null>(null);

  useEffect(() => {
    if (!TENANT_ID) return;
    fetch(`${API_BASE}/negotiation?tenant_id=${TENANT_ID}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : []))
      .then(setLines);
  }, []);

  if (!TENANT_ID) {
    return (
      <p className="text-sm text-gray-600">
        Set <code>NEXT_PUBLIC_DEV_TENANT_ID</code> in <code>frontend/.env.local</code>.
      </p>
    );
  }

  const totalAnnualized = (lines ?? []).reduce((sum, l) => sum + Number(l.annualized_savings), 0);

  return (
    <div className="max-w-4xl">
      <div className="mb-4 flex items-center justify-between print:hidden">
        <h1 className="text-xl font-semibold">Negotiation sheet</h1>
        <button
          onClick={() => window.print()}
          className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50"
        >
          Print
        </button>
      </div>

      {lines === null && <p className="text-sm text-gray-500">Loading...</p>}
      {lines !== null && lines.length === 0 && (
        <p className="text-sm text-gray-500">No overpriced SKUs with enough peer data right now.</p>
      )}
      {lines !== null && lines.length > 0 && (
        <>
          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b text-left text-gray-500">
                <th className="py-2 pr-4">SKU</th>
                <th className="py-2 pr-4">Current price</th>
                <th className="py-2 pr-4">Target price (peer p25)</th>
                <th className="py-2 pr-4">90d qty</th>
                <th className="py-2 pr-4">Recoverable (90d)</th>
                <th className="py-2 pr-4">Annualized savings</th>
              </tr>
            </thead>
            <tbody>
              {lines.map((line) => (
                <tr key={line.canonical_sku_id} className="border-b">
                  <td className="py-2 pr-4">{line.canonical_sku_name}</td>
                  <td className="py-2 pr-4">${line.current_price}</td>
                  <td className="py-2 pr-4">
                    ${line.target_price}{" "}
                    <span className="text-xs text-gray-400">({line.peer_distinct_tenant_count} peers)</span>
                  </td>
                  <td className="py-2 pr-4">{line.trailing_90d_quantity}</td>
                  <td className="py-2 pr-4">${line.recoverable_90d}</td>
                  <td className="py-2 pr-4 font-medium">${line.annualized_savings}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="mt-4 text-sm font-medium">
            Total annualized savings opportunity: ${totalAnnualized.toFixed(2)}
          </div>
        </>
      )}
    </div>
  );
}
