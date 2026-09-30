"use client";

import { useEffect, useMemo, useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { useHydrated } from "@/lib/useHydrated";

import { PERIODS, exportUrl, periodRange, type ExportFormat, type PeriodId, type Range } from "./exportPeriod";

type Distributor = { id: string; name: string };

/** Download this location's invoices, or their line items, as a spreadsheet
 *  (backend app/api/exports.py). */
export default function ExportPanel() {
  const locationId = useLocationId();
  const hydrated = useHydrated();
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState<"invoices" | "line-items">("invoices");
  // Excel unless asked: a CSV opened in Excel loses item codes' and invoice
  // numbers' leading zeros, which the workbook keeps.
  const [format, setFormat] = useState<ExportFormat>("xlsx");
  const [period, setPeriod] = useState<PeriodId>("last-month");
  const [custom, setCustom] = useState<Range>({ start: null, end: null });
  const [distributorId, setDistributorId] = useState("");
  const [distributors, setDistributors] = useState<Distributor[]>([]);

  useEffect(() => {
    if (!open || distributors.length || !locationId) return;
    // This location's list: the shared distributors and any it added.
    api(`/distributors?tenant_id=${locationId}`)
      .then((res) => (res.ok ? res.json() : []))
      .then(setDistributors)
      .catch(() => setDistributors([]));
  }, [open, distributors.length, locationId]);

  // Computed when opened, on the viewer's own calendar.
  const range = useMemo<Range>(
    () => (period === "custom" ? custom : periodRange(period, new Date())),
    [period, custom],
  );
  const backwards = !!(range.start && range.end && range.start > range.end);
  const downloadLabel = format === "xlsx" ? "Download Excel file" : "Download CSV";

  return (
    <>
      <button
        type="button"
        className="btn-secondary"
        aria-expanded={open}
        disabled={!hydrated}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden>⇩</span> Export
      </button>

      {open && (
        <section aria-label="Export" className="card order-last w-full border-l-4 border-l-brand-400 p-4" data-testid="export-panel">
          <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
            <h2 className="section-title">Export to a spreadsheet</h2>
            <p className="text-xs text-gray-500">Opens in Excel, Google Sheets, or your accounting software.</p>
          </div>

          <div className="mt-3 grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
            <Segmented
              legend="What"
              name="export-kind"
              value={kind}
              onChange={setKind}
              options={[
                ["invoices", "Invoices"],
                ["line-items", "Line items"],
              ]}
            />
            <Segmented
              legend="Format"
              name="export-format"
              value={format}
              onChange={setFormat}
              options={[
                ["xlsx", "Excel"],
                ["csv", "CSV"],
              ]}
            />

            <label className="block min-w-0 text-sm">
              <span className="mb-1 block text-gray-600">Invoice dates</span>
              <select
                className="input w-full"
                value={period}
                onChange={(e) => setPeriod(e.target.value as PeriodId)}
              >
                {PERIODS.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
            </label>

            <label className="block min-w-0 text-sm">
              <span className="mb-1 block text-gray-600">Distributor</span>
              <select className="input w-full" value={distributorId} onChange={(e) => setDistributorId(e.target.value)}>
                <option value="">All distributors</option>
                {distributors.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </label>

            {period === "custom" ? (
              <div className="grid min-w-0 grid-cols-2 gap-2">
                <label className="block min-w-0 text-sm">
                  <span className="mb-1 block text-gray-600">From</span>
                  <input
                    type="date"
                    className="input w-full min-w-0 px-2"
                    value={custom.start ?? ""}
                    onChange={(e) => setCustom((c) => ({ ...c, start: e.target.value || null }))}
                  />
                </label>
                <label className="block min-w-0 text-sm">
                  <span className="mb-1 block text-gray-600">To</span>
                  <input
                    type="date"
                    className="input w-full min-w-0 px-2"
                    value={custom.end ?? ""}
                    onChange={(e) => setCustom((c) => ({ ...c, end: e.target.value || null }))}
                  />
                </label>
              </div>
            ) : (
              <div className="min-w-0 text-sm">
                <span className="mb-1 block text-gray-600">Covers</span>
                <p className="num py-2 text-gray-900" data-testid="export-range">
                  {range.start && range.end ? `${range.start} to ${range.end}` : "Every invoice"}
                </p>
              </div>
            )}
          </div>

          {backwards && (
            <p role="alert" className="mt-3 text-sm text-red-600">
              The start date is after the end date.
            </p>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2">
            {backwards || !locationId ? (
              <button type="button" className="btn-primary" disabled>
                <span aria-hidden>⇩</span> {downloadLabel}
              </button>
            ) : (
              <a className="btn-primary" href={exportUrl(kind, locationId, range, distributorId, format)} download>
                <span aria-hidden>⇩</span> {downloadLabel}
              </a>
            )}
            <p className="min-w-0 text-xs text-gray-500">
              {format === "xlsx"
                ? "Item codes and invoice numbers stay exactly as printed. "
                : "For importing into accounting software. "}
              Includes invoices still waiting for review, marked in the Status column.
            </p>
          </div>
        </section>
      )}
    </>
  );
}

/** A pair of buttons that act as radio buttons. */
function Segmented<T extends string>({
  legend,
  name,
  value,
  onChange,
  options,
}: {
  legend: string;
  name: string;
  value: T;
  onChange: (value: T) => void;
  options: [T, string][];
}) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-1 text-sm text-gray-600">{legend}</legend>
      <div className="inline-flex rounded-md shadow-sm ring-1 ring-inset ring-gray-300">
        {options.map(([option, label], i) => (
          <label
            key={option}
            className={`cursor-pointer whitespace-nowrap px-3 py-2 text-sm font-semibold ${
              i === 0 ? "rounded-l-md" : ""
            } ${i === options.length - 1 ? "rounded-r-md" : ""} ${
              value === option ? "bg-brand-400 text-brand-950" : "bg-white text-gray-700 hover:bg-gray-50"
            }`}
          >
            <input
              type="radio"
              name={name}
              value={option}
              checked={value === option}
              onChange={() => onChange(option)}
              className="sr-only"
            />
            {label}
          </label>
        ))}
      </div>
    </fieldset>
  );
}
