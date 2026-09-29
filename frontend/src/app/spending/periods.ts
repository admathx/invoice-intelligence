/** Turning months of spending into the periods the page compares. */

export type MonthSpend = {
  month: string; // "2026-08"
  total: string;
  invoice_count: number;
  by_category: Record<string, string>;
  by_distributor: Record<string, string>;
};

export type Period =
  | { kind: "this-month" }
  | { kind: "last-month" }
  | { kind: "last-3" }
  | { kind: "last-12" }
  | { kind: "month"; month: string };

export type Breakdown = { name: string; amount: number; share: number; change: number | null };

export type PeriodSummary = {
  label: string;
  /** What the change is against: "Jul", "the 3 months before". */
  previousLabel: string;
  total: number;
  previousTotal: number | null;
  categories: Breakdown[];
  distributors: Breakdown[];
};

const MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

export function monthLabel(month: string, style: "long" | "short" = "long"): string {
  const [y, m] = month.split("-").map(Number);
  const name = MONTH_NAMES[m - 1];
  return style === "long" ? `${name} ${y}` : name.slice(0, 3);
}

/** Which months (by index into `months`, oldest first) a period covers, and
 *  the same number of months just before it, for the comparison. The last
 *  month in `months` is the current one. */
export function periodMonths(months: MonthSpend[], period: Period): { now: number[]; before: number[] } {
  const last = months.length - 1;
  const span = (end: number, length: number) => {
    const idx: number[] = [];
    for (let i = end - length + 1; i <= end; i++) if (i >= 0) idx.push(i);
    return idx;
  };
  let end = last;
  let length = 1;
  if (period.kind === "last-month") end = last - 1;
  if (period.kind === "last-3") [end, length] = [last - 1, 3];
  if (period.kind === "last-12") [end, length] = [last - 1, 12];
  if (period.kind === "month") end = months.findIndex((m) => m.month === period.month);
  if (end < 0) return { now: [], before: [] };
  return { now: span(end, length), before: end - length >= 0 ? span(end - length, length) : [] };
}

export function periodLabel(months: MonthSpend[], period: Period): string {
  switch (period.kind) {
    case "this-month":
      return "this month so far";
    case "last-month":
      return months.length > 1 ? monthLabel(months[months.length - 2].month) : "last month";
    case "last-3":
      return "the last 3 months";
    case "last-12":
      return "the last 12 months";
    case "month":
      return monthLabel(period.month);
  }
}

function sum(months: MonthSpend[], idx: number[], pick: (m: MonthSpend) => Record<string, string>): Map<string, number> {
  const out = new Map<string, number>();
  for (const i of idx) for (const [k, v] of Object.entries(pick(months[i]))) out.set(k, (out.get(k) ?? 0) + Number(v));
  return out;
}

function breakdown(now: Map<string, number>, before: Map<string, number> | null, total: number): Breakdown[] {
  return [...now.entries()]
    .map(([name, amount]) => {
      const prior = before?.get(name) ?? 0;
      return { name, amount, share: total ? amount / total : 0, change: before && prior > 0 ? amount / prior - 1 : null };
    })
    .sort((a, b) => b.amount - a.amount);
}

export function summarize(months: MonthSpend[], period: Period): PeriodSummary {
  const { now, before } = periodMonths(months, period);
  const total = now.reduce((t, i) => t + Number(months[i].total), 0);
  // A comparison only where the earlier stretch is complete and had spending:
  // "up 100%" against months before the first invoice would be noise.
  const comparable = before.length === now.length && before.length > 0;
  const previousTotal = comparable ? before.reduce((t, i) => t + Number(months[i].total), 0) : null;
  const usable = previousTotal ? before : null;
  return {
    label: periodLabel(months, period),
    previousLabel:
      before.length === 1 ? monthLabel(months[before[0]].month, "short") : `the ${before.length} months before`,
    total,
    previousTotal: previousTotal || null,
    categories: breakdown(sum(months, now, (m) => m.by_category), usable && sum(months, usable, (m) => m.by_category), total),
    distributors: breakdown(
      sum(months, now, (m) => m.by_distributor),
      usable && sum(months, usable, (m) => m.by_distributor),
      total,
    ),
  };
}

/** A round number just above the tallest bar, for the chart's top line. */
export function niceCeiling(max: number): number {
  if (max <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(max));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (step * magnitude >= max) return step * magnitude;
  return 10 * magnitude;
}

/** "$40k", "$1.2M", "$800": axis labels, where cents are noise. */
export function compactMoney(value: number): string {
  if (value >= 1_000_000) return `$${(value / 1_000_000).toFixed(value % 1_000_000 ? 1 : 0)}M`;
  if (value >= 1000) return `$${(value / 1000).toFixed(value % 1000 ? 1 : 0).replace(/\.0$/, "")}k`;
  return `$${Math.round(value)}`;
}
