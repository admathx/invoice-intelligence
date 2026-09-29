"use client";

import { useState } from "react";

import StatTile from "@/components/StatTile";
import { money, percent, priceChangeTone, TONE_TEXT } from "@/lib/format";

import {
  compactMoney,
  monthLabel,
  niceCeiling,
  summarize,
  type Breakdown,
  type MonthSpend,
  type Period,
} from "./periods";

const PERIODS: { period: Period; label: string }[] = [
  { period: { kind: "this-month" }, label: "This month" },
  { period: { kind: "last-month" }, label: "Last month" },
  { period: { kind: "last-3" }, label: "Last 3 months" },
  { period: { kind: "last-12" }, label: "Last 12 months" },
];

const same = (a: Period, b: Period) =>
  a.kind === b.kind && (a.kind !== "month" || (b.kind === "month" && a.month === b.month));

/** Up is red and down is green, as for prices everywhere else: this is
 *  money going out. */
function Change({ value, against }: { value: number | null; against?: string }) {
  if (value === null || !Number.isFinite(value)) return <span className="text-gray-400">—</span>;
  if (Math.abs(value) < 0.0005) return <span className="text-gray-500">no change{against ? ` vs ${against}` : ""}</span>;
  return (
    <span className={`whitespace-nowrap font-semibold ${TONE_TEXT[priceChangeTone(value)]}`}>
      {value > 0 ? "▲" : "▼"} {percent(Math.abs(value))}
      {against && <span className="font-normal text-gray-500"> vs {against}</span>}
    </span>
  );
}

export default function SpendingView({ months }: { months: MonthSpend[] }) {
  const [period, setPeriod] = useState<Period>(months.length > 1 ? { kind: "last-month" } : { kind: "this-month" });
  const current = months[months.length - 1];
  const previous = months.length > 1 ? months[months.length - 2] : null;
  const beforeThat = months.length > 2 ? months[months.length - 3] : null;
  // Every finished month, a month with nothing in it included: the chart
  // shows it, and leaving it out would make the average something else.
  // (The months start at the first with any spending, so these are real.)
  const complete = months.slice(0, -1);
  const average = complete.length ? complete.reduce((t, m) => t + Number(m.total), 0) / complete.length : null;
  const summary = summarize(months, period);

  return (
    <>
      <div className="mb-5 grid gap-3 sm:grid-cols-3">
        <StatTile label="This month so far" value={money(current.total)}>
          {current.invoice_count} invoice{current.invoice_count === 1 ? "" : "s"}
        </StatTile>
        <StatTile label={previous ? monthLabel(previous.month) : "Last month"} value={previous ? money(previous.total) : "—"}>
          {previous && beforeThat && Number(beforeThat.total) > 0 ? (
            <Change
              value={Number(previous.total) / Number(beforeThat.total) - 1}
              against={monthLabel(beforeThat.month, "short")}
            />
          ) : (
            "Nothing to compare with yet"
          )}
        </StatTile>
        <StatTile label="Average month" value={average === null ? "—" : money(average)}>
          {complete.length ? `over ${complete.length} month${complete.length === 1 ? "" : "s"}` : "Needs a full month"}
        </StatTile>
      </div>

      <MonthlyChart months={months} selected={period} onSelect={(month) => setPeriod({ kind: "month", month })} />

      <section className="mt-6" aria-labelledby="where-it-went">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 id="where-it-went" className="section-title">
            Where it went: {summary.label}
          </h2>
          <div className="flex max-w-full overflow-x-auto rounded-lg bg-gray-100 p-1 text-sm" role="group" aria-label="Period">
            {PERIODS.map((p) => (
              <button
                key={p.label}
                type="button"
                aria-pressed={same(period, p.period)}
                onClick={() => setPeriod(p.period)}
                className={`whitespace-nowrap rounded-md px-3 py-1.5 font-medium transition-colors ${
                  same(period, p.period) ? "bg-white text-brand-800 shadow-sm ring-1 ring-brand-300" : "text-gray-600 hover:text-gray-900"
                }`}
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>
        <p className="mb-3 text-sm text-gray-600">
          <strong className="num text-gray-900">{money(summary.total)}</strong> spent in {summary.label}
          {summary.previousTotal !== null && (
            <>
              {" · "}
              <Change value={summary.total / summary.previousTotal - 1} against={summary.previousLabel} />
            </>
          )}
        </p>
        {summary.total === 0 ? (
          <p className="card px-4 py-4 text-sm text-gray-500">Nothing spent in {summary.label}.</p>
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
            <BreakdownCard title="By category" rows={summary.categories} />
            <BreakdownCard title="By distributor" rows={summary.distributors} />
          </div>
        )}
      </section>
    </>
  );
}

/** One bar per month, the selected one darker. Each bar is a button: click it
 *  to see where that month's money went. Hovering (or focusing) shows the
 *  month's figures; a table carries the same numbers for screen readers. */
function MonthlyChart({
  months,
  selected,
  onSelect,
}: {
  months: MonthSpend[];
  selected: Period;
  onSelect: (month: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const top = niceCeiling(Math.max(...months.map((m) => Number(m.total))));
  const ticks = [top, top / 2, 0];
  const current = months.length - 1;
  const selectedIndex =
    selected.kind === "month"
      ? months.findIndex((m) => m.month === selected.month)
      : selected.kind === "this-month"
        ? current
        : selected.kind === "last-month"
          ? current - 1
          : -1;

  return (
    <section className="card p-4" aria-labelledby="each-month">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="each-month" className="section-title">
          Each month
        </h2>
        <p className="text-xs text-gray-500">Click a month to see where it went.</p>
      </div>
      <div className="mt-4 flex gap-2">
        {/* The scale: three quiet lines are enough to read a bar against. */}
        <div className="num relative w-10 flex-none text-right text-[11px] text-gray-500" aria-hidden>
          {ticks.map((t, i) => (
            <span key={t} className="absolute right-0 -translate-y-1/2" style={{ top: `${(i / (ticks.length - 1)) * 100}%` }}>
              {compactMoney(t)}
            </span>
          ))}
        </div>
        <div className="relative h-48 min-w-0 flex-1">
          {ticks.map((t, i) => (
            <div
              key={t}
              aria-hidden
              className={`absolute inset-x-0 border-t ${t === 0 ? "border-gray-300" : "border-dashed border-gray-200"}`}
              style={{ top: `${(i / (ticks.length - 1)) * 100}%` }}
            />
          ))}
          <div className="absolute inset-0 flex items-end gap-0.5">
            {months.map((m, i) => {
              const total = Number(m.total);
              const height = top ? (total / top) * 100 : 0;
              const isSelected = i === selectedIndex;
              return (
                <button
                  key={m.month}
                  type="button"
                  onClick={() => onSelect(m.month)}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                  aria-label={`${monthLabel(m.month)}: ${money(m.total)}${i === current ? " so far" : ""}`}
                  aria-pressed={isSelected}
                  // The whole column is the target, not just the bar: a
                  // short month is still easy to hit.
                  className="group relative flex h-full min-w-0 flex-1 items-end justify-center rounded-sm focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand-600"
                >
                  <span
                    className={`block w-full max-w-[3rem] rounded-t transition-colors ${
                      isSelected ? "bg-brand-800" : "bg-brand-600 group-hover:bg-brand-700"
                    } ${i === current ? "opacity-60" : ""}`}
                    style={{ height: `${Math.max(height, total > 0 ? 1 : 0)}%` }}
                  />
                </button>
              );
            })}
          </div>
          {hover !== null && (
            <div
              role="tooltip"
              className="pointer-events-none absolute z-10 -translate-x-1/2 whitespace-nowrap rounded-md bg-gray-900 px-2.5 py-1.5 text-xs text-white shadow-lg"
              style={{
                left: `${((hover + 0.5) / months.length) * 100}%`,
                bottom: `${Math.min(((Number(months[hover].total) / top) * 100) + 4, 92)}%`,
              }}
            >
              <div className="font-semibold">
                {monthLabel(months[hover].month)}
                {hover === current ? " (so far)" : ""}
              </div>
              <div className="num">
                {money(months[hover].total)} · {months[hover].invoice_count} invoice
                {months[hover].invoice_count === 1 ? "" : "s"}
              </div>
            </div>
          )}
        </div>
      </div>
      <div className="ml-12 mt-1 flex gap-0.5" aria-hidden>
        {months.map((m, i) => (
          <span
            key={m.month}
            className={`min-w-0 flex-1 truncate text-center text-[11px] ${i === selectedIndex ? "font-semibold text-gray-900" : "text-gray-500"}`}
          >
            {monthLabel(m.month, "short")}
          </span>
        ))}
      </div>
      <table className="sr-only">
        <caption>Spending each month, before tax</caption>
        <thead>
          <tr>
            <th>Month</th>
            <th>Spent</th>
            <th>Invoices</th>
          </tr>
        </thead>
        <tbody>
          {months.map((m) => (
            <tr key={m.month}>
              <td>{monthLabel(m.month)}</td>
              <td>{money(m.total)}</td>
              <td>{m.invoice_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function BreakdownCard({ title, rows }: { title: string; rows: Breakdown[] }) {
  const max = Math.max(...rows.map((r) => r.amount), 1);
  return (
    <div className="card p-4">
      <h3 className="text-sm font-semibold text-gray-900">{title}</h3>
      <table className="mt-2 w-full text-sm">
        <thead className="sr-only">
          <tr>
            <th>{title.replace("By ", "")} and share</th>
            <th>Spent</th>
            <th>Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const unmatched = row.name === "Not matched yet" || row.name === "Distributor not known";
            return (
              <tr key={row.name} className="align-top">
                <td className="w-full py-1.5 pr-3">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className={`min-w-0 [overflow-wrap:anywhere] ${unmatched ? "text-gray-500" : "font-medium"}`}>
                      {row.name}
                      {row.name === "Not matched yet" && (
                        <>
                          {" · "}
                          <a href="/review" className="link font-normal">
                            match them
                          </a>
                        </>
                      )}
                    </span>
                    <span className="num text-xs text-gray-500">{percent(row.share, { places: 0 })}</span>
                  </div>
                  <div className="mt-1 h-2 overflow-hidden rounded-full bg-gray-100" aria-hidden>
                    <div
                      className={`h-full rounded-full ${unmatched ? "bg-gray-400" : "bg-brand-600"}`}
                      style={{ width: `${(row.amount / max) * 100}%` }}
                    />
                  </div>
                </td>
                <td className="num whitespace-nowrap py-1.5 pr-3 text-right font-semibold text-gray-900">{money(row.amount)}</td>
                <td className="num whitespace-nowrap py-1.5 text-right text-xs">
                  <Change value={row.change} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
