"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

import { isoDate } from "../invoices/exportPeriod";
import type { MonthSpend } from "./periods";
import SpendingView from "./SpendingView";

type Spending = { months: MonthSpend[]; not_counted: number };

/** Fetched from the browser, with the browser's date: "this month" is the
 *  viewer's month, not the server's (UTC), which turned over hours early in
 *  the evening across the US, and disagreed with the Export panel. */
export default function SpendingLoader({ locationId }: { locationId: string }) {
  const [data, setData] = useState<Spending | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api(`/spending?tenant_id=${locationId}&today=${isoDate(new Date())}`)
      .then((res) => {
        if (!res.ok) throw new Error(`spending: ${res.status}`);
        return res.json();
      })
      .then((body: Spending) => !cancelled && setData(body))
      .catch(() => !cancelled && setFailed(true));
    return () => {
      cancelled = true;
    };
  }, [locationId]);

  if (failed) {
    return <p className="card px-4 py-4 text-sm text-gray-600">Couldn&rsquo;t load your spending. Try again in a moment.</p>;
  }
  if (!data) return <p className="text-sm text-gray-500">Loading…</p>;
  const anything = data.months.some((m) => Number(m.total) > 0);
  return (
    <>
      {data.not_counted > 0 && (
        <p className="mb-4 rounded-lg border border-amber-200 border-l-4 border-l-amber-400 bg-amber-50 px-4 py-2.5 text-sm text-amber-900">
          {data.not_counted === 1 ? "1 invoice needs" : `${data.not_counted} invoices need`} a look and{" "}
          {data.not_counted === 1 ? "isn't" : "aren't"} counted here yet.{" "}
          <a href="/invoices" className="font-semibold underline">
            See invoices
          </a>
        </p>
      )}
      {anything ? (
        <SpendingView months={data.months} />
      ) : (
        <p className="card px-4 py-4 text-sm text-gray-600">
          Nothing to show yet. Spending shows up here once your first invoices are read.{" "}
          <a href="/invoices" className="link">
            Add an invoice
          </a>
        </p>
      )}
    </>
  );
}
