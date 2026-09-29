/** The periods the export offers, as inclusive invoice-date ranges. */

export const PERIODS = [
  { id: "this-month", label: "This month" },
  { id: "last-month", label: "Last month" },
  { id: "this-quarter", label: "This quarter" },
  { id: "last-quarter", label: "Last quarter" },
  { id: "year-to-date", label: "Year to date" },
  { id: "last-year", label: "Last year" },
  { id: "all", label: "All time" },
  { id: "custom", label: "Choose dates…" },
] as const;

export type PeriodId = (typeof PERIODS)[number]["id"];
export type Range = { start: string | null; end: string | null };

/** YYYY-MM-DD in local time: the calendar the person is looking at, not UTC's. */
export function isoDate(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function monthRange(year: number, month: number, months = 1): Range {
  // Day 0 of the month after is the last day of the range.
  return { start: isoDate(new Date(year, month, 1)), end: isoDate(new Date(year, month + months, 0)) };
}

export function periodRange(period: Exclude<PeriodId, "custom">, today: Date): Range {
  const year = today.getFullYear();
  const month = today.getMonth();
  const quarterStart = month - (month % 3);
  switch (period) {
    case "this-month":
      return monthRange(year, month);
    case "last-month":
      return monthRange(year, month - 1);
    case "this-quarter":
      return monthRange(year, quarterStart, 3);
    case "last-quarter":
      return monthRange(year, quarterStart - 3, 3);
    case "year-to-date":
      return { start: isoDate(new Date(year, 0, 1)), end: isoDate(today) };
    case "last-year":
      return monthRange(year - 1, 0, 12);
    case "all":
      return { start: null, end: null };
  }
}

export type ExportFormat = "xlsx" | "csv";

export function exportUrl(
  kind: "invoices" | "line-items",
  locationId: string,
  range: Range,
  distributorId: string,
  format: ExportFormat,
): string {
  const params = new URLSearchParams({ tenant_id: locationId, format });
  if (range.start) params.set("start", range.start);
  if (range.end) params.set("end", range.end);
  if (distributorId) params.set("distributor_id", distributorId);
  return `/api/exports/${kind}?${params}`;
}
