"use client";

import { useMemo, useState } from "react";

import AddLineModal from "./AddLineModal";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const TENANT_ID = process.env.NEXT_PUBLIC_DEV_TENANT_ID ?? "";

type LineItem = {
  id: string;
  line_number: number;
  raw_description: string;
  raw_pack_size: string | null;
  quantity: string;
  unit_price: string;
  extended_price: string;
  uom: string;
  normalized_unit_price: string | null;
  base_uom: string | null;
  review_status: string;
};

export type InvoiceDetail = {
  id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  distributor_id: string | null;
  distributor_name: string | null;
  status: string;
  subtotal: string | null;
  tax: string | null;
  total: string | null;
  line_items: LineItem[];
  page_image_urls: string[];
  check: { passes: boolean; reasons: string[]; failed_line_numbers: number[] };
};

export type Distributor = { id: string; name: string; slug: string };

type LineField = "quantity" | "unit_price" | "extended_price";
type HeaderField = "subtotal" | "tax" | "total" | "invoice_date" | "distributor_id";

// Money stays a string end to end: typed in, sent to the API, parsed there as
// a Decimal (SPEC.md §11). Nothing here does arithmetic on it; the check that
// decides whether the numbers add up runs server-side, as the same rules the
// worker applied.
function sameMoney(a: string, b: string | null): boolean {
  if (b === null) return a === "";
  const [x, y] = [Number(a), Number(b)];
  return a.trim() !== "" && Number.isFinite(x) && x === y;
}

export default function InvoiceReview({ initial, distributors }: { initial: InvoiceDetail; distributors: Distributor[] }) {
  const [invoice, setInvoice] = useState(initial);
  const [lineDrafts, setLineDrafts] = useState<Record<string, Partial<Record<LineField, string>>>>({});
  const [headerDrafts, setHeaderDrafts] = useState<Partial<Record<HeaderField, string>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `failed` is editable too: extraction produced nothing usable, and typing
  // the lines in from the image is how it's recovered. The server moves it to
  // needs_review on the first edit.
  const editable = invoice.status === "needs_review" || invoice.status === "failed";
  const [adding, setAdding] = useState(false);
  // Extraction's "other" is stored as a real distributor row but isn't in the
  // picker (it isn't a choice), so it reads as unrecognized here, same as NULL.
  const recognized = distributors.some((d) => d.id === invoice.distributor_id);

  // Only values that actually differ from what the server holds count as
  // edits. Confirming is disabled while any exist: the check on screen is the
  // check of the SAVED numbers, and confirming unsaved ones would confirm
  // something other than what was checked.
  const edits = useMemo(() => {
    const lines = invoice.line_items.flatMap((li) => {
      const draft = lineDrafts[li.id] ?? {};
      const changed = (Object.keys(draft) as LineField[]).filter((f) => !sameMoney(draft[f] ?? "", li[f]));
      return changed.length ? [{ id: li.id, ...Object.fromEntries(changed.map((f) => [f, draft[f]])) }] : [];
    });
    const header = (Object.keys(headerDrafts) as HeaderField[]).filter((f) => {
      const value = headerDrafts[f] ?? "";
      return f === "invoice_date" || f === "distributor_id"
        ? value !== "" && value !== (invoice[f] ?? "")
        : !sameMoney(value, invoice[f]);
    });
    return { lines, header };
  }, [invoice, lineDrafts, headerDrafts]);
  const dirty = edits.lines.length > 0 || edits.header.length > 0;

  async function send(method: "PATCH" | "POST", path: string, body?: unknown) {
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoice.id}${path}?tenant_id=${TENANT_ID}`, {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const detail = data?.detail;
        setError(
          typeof detail === "string"
            ? detail
            : detail?.reasons
              ? `${detail.message}: ${detail.reasons.join("; ")}`
              : "Request failed — check the values and try again."
        );
        return;
      }
      setInvoice(data);
      setLineDrafts({});
      setHeaderDrafts({});
    } catch {
      setError("Couldn't reach the server. Nothing was saved.");
    } finally {
      setBusy(false);
    }
  }

  async function removeLine(li: LineItem) {
    if (!window.confirm(`Remove line ${li.line_number} (${li.raw_description})?`)) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/invoices/${invoice.id}/line-items/${li.id}?tenant_id=${TENANT_ID}`, {
        method: "DELETE",
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(typeof data?.detail === "string" ? data.detail : "Couldn't remove the line.");
        return;
      }
      setInvoice(data);
      setLineDrafts(({ [li.id]: _removed, ...rest }) => rest);
    } catch {
      setError("Couldn't reach the server. Nothing was removed.");
    } finally {
      setBusy(false);
    }
  }

  function save() {
    const body: Record<string, unknown> = { line_items: edits.lines };
    for (const f of edits.header) body[f] = headerDrafts[f];
    void send("PATCH", "", body);
  }

  const failed = new Set(invoice.check.failed_line_numbers);
  const lineValue = (li: LineItem, f: LineField) => lineDrafts[li.id]?.[f] ?? li[f];
  const headerValue = (f: HeaderField) => headerDrafts[f] ?? invoice[f] ?? "";
  const moneyInput = "w-24 rounded border border-gray-300 px-1.5 py-0.5 text-right tabular-nums";

  return (
    <div>
      <div className="mb-1 flex items-baseline gap-3">
        <h1 className="text-xl font-semibold">{invoice.invoice_number ?? invoice.id}</h1>
        <StatusBadge status={invoice.status} />
      </div>
      <p className="mb-4 text-sm text-gray-500">
        {invoice.distributor_name ?? "Unrecognized distributor"} · {invoice.invoice_date ?? "no date"}
      </p>

      {editable && (
        <div className="mb-4 rounded border border-amber-200 bg-amber-50 px-4 py-3 text-sm">
          <p className="font-medium text-amber-900">
            {invoice.status === "failed"
              ? "This invoice couldn't be read automatically, so nothing from it is in your analytics. Enter its lines from the invoice image."
              : invoice.check.passes
                ? "The numbers add up now. Confirm to let this invoice's prices into your analytics."
                : "This invoice's numbers don't add up, so its prices are being kept out of your analytics."}
          </p>
          {!invoice.check.passes && (
            <>
              <ul className="mt-1 list-disc pl-5 text-amber-800">
                {invoice.check.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
              <p className="mt-2 text-amber-800">
                {invoice.line_items.length === 0
                  ? "Add each line from the invoice image, enter its totals, then save."
                  : "Compare the highlighted lines with the invoice image, correct what was misread, then save."}
              </p>
            </>
          )}
        </div>
      )}
      {invoice.status === "confirmed" && (
        <p className="mb-4 rounded border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm text-emerald-800">
          Confirmed after review. Its prices now feed your benchmarks, alerts and negotiation sheet.
        </p>
      )}

      <div className="flex gap-6">
        <div className="w-72 flex-none">
          {invoice.page_image_urls.length > 0 ? (
            <div className="space-y-3">
              {invoice.page_image_urls.map((url) => (
                // eslint-disable-next-line @next/next/no-img-element
                <img key={url} src={`${API_BASE}${url}`} alt="Invoice page" className="w-full rounded border border-gray-200" />
              ))}
            </div>
          ) : (
            <p className="text-sm text-gray-400">No page image available for this invoice.</p>
          )}
        </div>

        <div className="flex-1">
          {editable && (
            <div className="mb-3 flex flex-wrap gap-4 text-sm">
              <label className="flex items-center gap-2">
                Distributor
                <select
                  value={headerDrafts.distributor_id ?? (recognized ? invoice.distributor_id ?? "" : "")}
                  onChange={(e) => setHeaderDrafts((d) => ({ ...d, distributor_id: e.target.value }))}
                  className="rounded border border-gray-300 px-2 py-1"
                >
                  {!recognized && <option value="">Unrecognized — choose</option>}
                  {distributors.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-2">
                Invoice date
                <input
                  type="date"
                  value={headerValue("invoice_date")}
                  onChange={(e) => setHeaderDrafts((d) => ({ ...d, invoice_date: e.target.value }))}
                  className="rounded border border-gray-300 px-2 py-1"
                />
              </label>
            </div>
          )}

          <table className="w-full border-collapse text-sm">
            <thead>
              <tr className="border-b text-left text-gray-500">
                <th className="py-2 pr-3">#</th>
                <th className="py-2 pr-3">Description</th>
                <th className="py-2 pr-3 text-right">Qty</th>
                <th className="py-2 pr-3">UOM</th>
                <th className="py-2 pr-3 text-right">Unit price</th>
                <th className="py-2 pr-3 text-right">Extended</th>
                <th className="py-2 pr-3">Review</th>
                {editable && <th className="py-2" />}
              </tr>
            </thead>
            <tbody>
              {invoice.line_items.map((li) => {
                const flagged = failed.has(li.line_number) && !edits.lines.some((e) => e.id === li.id);
                return (
                  <tr key={li.id} className={`border-b ${flagged ? "bg-red-50" : ""}`}>
                    <td className="py-1.5 pr-3">{li.line_number}</td>
                    <td className="py-1.5 pr-3">
                      {li.raw_description}
                      {flagged && <div className="text-xs text-red-700">qty × unit price ≠ extended</div>}
                    </td>
                    {(["quantity"] as LineField[]).map((f) => (
                      <td key={f} className="py-1.5 pr-3 text-right">
                        {editable ? (
                          <input
                            aria-label={`Line ${li.line_number} ${f}`}
                            inputMode="decimal"
                            value={lineValue(li, f)}
                            onChange={(e) =>
                              setLineDrafts((d) => ({ ...d, [li.id]: { ...d[li.id], [f]: e.target.value } }))
                            }
                            className={moneyInput}
                          />
                        ) : (
                          li[f]
                        )}
                      </td>
                    ))}
                    <td className="py-1.5 pr-3">{li.uom}</td>
                    {(["unit_price", "extended_price"] as LineField[]).map((f) => (
                      <td key={f} className="py-1.5 pr-3 text-right">
                        {editable ? (
                          <input
                            aria-label={`Line ${li.line_number} ${f.replace("_", " ")}`}
                            inputMode="decimal"
                            value={lineValue(li, f)}
                            onChange={(e) =>
                              setLineDrafts((d) => ({ ...d, [li.id]: { ...d[li.id], [f]: e.target.value } }))
                            }
                            className={moneyInput}
                          />
                        ) : (
                          `$${li[f]}`
                        )}
                      </td>
                    ))}
                    <td className="py-1.5 pr-3 text-gray-500">{li.review_status}</td>
                    {editable && (
                      <td className="py-1.5 text-right">
                        <button
                          onClick={() => void removeLine(li)}
                          disabled={busy}
                          aria-label={`Remove line ${li.line_number}`}
                          className="text-xs text-gray-400 hover:text-red-700 disabled:opacity-50"
                        >
                          Remove
                        </button>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
          {invoice.line_items.length === 0 && (
            <p className="py-4 text-sm text-gray-500">
              No line items were detected on this invoice.{editable && " Add them from the invoice image."}
            </p>
          )}
          {editable && (
            <button
              onClick={() => setAdding(true)}
              disabled={busy}
              className="mt-2 text-sm font-medium text-blue-700 hover:underline disabled:opacity-50"
            >
              + Add line item
            </button>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-4 text-sm text-gray-700">
            {(["subtotal", "tax", "total"] as HeaderField[]).map((f) => (
              <label key={f} className="flex items-center gap-2 capitalize">
                {f}
                {editable ? (
                  <input
                    aria-label={f}
                    inputMode="decimal"
                    value={headerValue(f)}
                    onChange={(e) => setHeaderDrafts((d) => ({ ...d, [f]: e.target.value }))}
                    className={moneyInput}
                  />
                ) : (
                  <span className="tabular-nums">${invoice[f] ?? "—"}</span>
                )}
              </label>
            ))}
          </div>

          {editable && (
            <div className="mt-4 flex items-center gap-3">
              <button
                onClick={save}
                disabled={busy || !dirty}
                className="rounded border border-gray-300 bg-white px-3 py-1.5 text-sm hover:bg-gray-50 disabled:opacity-50"
              >
                Save &amp; re-check
              </button>
              <button
                onClick={() => void send("POST", "/confirm")}
                disabled={busy || dirty || !invoice.check.passes}
                title={dirty ? "Save your changes first" : !invoice.check.passes ? "The numbers still don't add up" : ""}
                className="rounded bg-gray-900 px-3 py-1.5 text-sm text-white hover:bg-gray-800 disabled:opacity-40"
              >
                Confirm invoice
              </button>
              {dirty && <span className="text-xs text-gray-500">Unsaved changes</span>}
            </div>
          )}
          {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
        </div>
      </div>

      {adding && (
        <AddLineModal
          invoiceId={invoice.id}
          onClose={() => setAdding(false)}
          // Keeps any unsaved edits to other lines: they're keyed by line id,
          // which a new line doesn't disturb.
          onAdded={(updated) => {
            setInvoice(updated);
            setAdding(false);
          }}
        />
      )}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const style =
    status === "needs_review"
      ? "bg-amber-100 text-amber-800"
      : status === "confirmed" || status === "extracted"
        ? "bg-emerald-100 text-emerald-800"
        : status === "failed"
          ? "bg-red-100 text-red-800"
          : "bg-gray-100 text-gray-700";
  return <span className={`rounded px-2 py-0.5 text-xs font-medium ${style}`}>{status.replace("_", " ")}</span>;
}
