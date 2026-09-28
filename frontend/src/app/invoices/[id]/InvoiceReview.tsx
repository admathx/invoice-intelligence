"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import { editableNumber, money, quantity, REVIEW_BADGE, STATUS_BADGE, STATUS_LABEL, unitPrice } from "@/lib/format";

import AddLineModal from "./AddLineModal";

type LineItem = {
  id: string;
  line_number: number;
  raw_description: string;
  raw_sku: string | null;
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

type MoneyField = "quantity" | "unit_price" | "extended_price";
// What identifies the item. Editable because OCR misreads these too, and the
// arithmetic check never looks at them: a pack read as "4/3 LB" for "4/5 LB"
// would otherwise confirm a per-pound price 67% too high.
type TextField = "raw_description" | "raw_sku" | "raw_pack_size" | "uom";
type LineField = MoneyField | TextField;
const MONEY_FIELDS: readonly LineField[] = [
  "quantity",
  "unit_price",
  "extended_price",
];

function sameValue(
  field: LineField,
  draft: string,
  saved: string | null,
): boolean {
  if (MONEY_FIELDS.includes(field)) return sameMoney(draft, saved);
  const norm = (v: string) =>
    field === "uom" ? v.trim().toUpperCase() : v.trim();
  return norm(draft) === norm(saved ?? "");
}
type HeaderField =
  "subtotal" | "tax" | "total" | "invoice_date" | "distributor_id";

// Money stays a string end to end: typed in, sent to the API, parsed there as
// a Decimal (SPEC.md §11). Nothing here does arithmetic on it; the check that
// decides whether the numbers add up runs server-side, as the same rules the
// worker applied.
function sameMoney(a: string, b: string | null): boolean {
  if (b === null) return a === "";
  const [x, y] = [Number(a), Number(b)];
  return a.trim() !== "" && Number.isFinite(x) && x === y;
}

export default function InvoiceReview({
  initial,
  distributors,
}: {
  initial: InvoiceDetail;
  distributors: Distributor[];
}) {
  const router = useRouter();
  const locationId = useLocationId();
  const [invoice, setInvoice] = useState(initial);
  const [lineDrafts, setLineDrafts] = useState<
    Record<string, Partial<Record<LineField, string>>>
  >({});
  const [headerDrafts, setHeaderDrafts] = useState<
    Partial<Record<HeaderField, string>>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // `failed` is editable too: extraction produced nothing usable, and typing
  // the lines in from the image is how it's recovered. The server moves it to
  // needs_review on the first edit.
  const editable =
    invoice.status === "needs_review" || invoice.status === "failed";
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
      const changed = (Object.keys(draft) as LineField[]).filter(
        (f) => !sameValue(f, draft[f] ?? "", li[f]),
      );
      return changed.length
        ? [
            {
              id: li.id,
              ...Object.fromEntries(changed.map((f) => [f, draft[f]])),
            },
          ]
        : [];
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
      const res = await api(
        `/invoices/${invoice.id}${path}?tenant_id=${locationId}`,
        {
          method,
          headers: body ? { "Content-Type": "application/json" } : undefined,
          body: body ? JSON.stringify(body) : undefined,
        },
      );
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        // A validation error points at line_items[i], a position in the list
        // of edits just sent, not a line number. Map it back so the message
        // names the row the person is looking at.
        const sentLines = edits.lines;
        setError(
          formatApiError(
            data?.detail,
            "Request failed — check the values and try again.",
            (loc) => {
              const i = loc.indexOf("line_items");
              if (i === -1 || typeof loc[i + 1] !== "number") return null;
              const line = invoice.line_items.find(
                (li) => li.id === sentLines[loc[i + 1] as number]?.id,
              );
              const field = loc[i + 2];
              return line
                ? `line ${line.line_number} ${String(field ?? "").replaceAll("_", " ")}`.trim()
                : null;
            },
          ),
        );
        return;
      }
      setInvoice(data);
      setLineDrafts({});
      setHeaderDrafts({});
      router.refresh(); // the history below the form
    } catch {
      setError("Couldn't reach the server. Nothing was saved.");
    } finally {
      setBusy(false);
    }
  }

  async function removeLine(li: LineItem) {
    if (
      !window.confirm(`Remove line ${li.line_number} (${li.raw_description})?`)
    )
      return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(
        `/invoices/${invoice.id}/line-items/${li.id}?tenant_id=${locationId}`,
        {
          method: "DELETE",
        },
      );
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(formatApiError(data?.detail, "Couldn't remove the line."));
        return;
      }
      setInvoice(data);
      setLineDrafts(({ [li.id]: _removed, ...rest }) => rest);
      router.refresh();
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
  const lineValue = (li: LineItem, f: LineField) =>
    lineDrafts[li.id]?.[f] ??
    (f === "quantity"
      ? editableNumber(li[f], "quantity")
      : f === "unit_price" || f === "extended_price"
        ? editableNumber(li[f], "money")
        : (li[f] ?? ""));
  const setLine =
    (li: LineItem, f: LineField) => (e: React.ChangeEvent<HTMLInputElement>) =>
      setLineDrafts((d) => ({
        ...d,
        [li.id]: { ...d[li.id], [f]: e.target.value },
      }));
  const textInput = "input px-1.5 py-0.5";
  const headerValue = (f: HeaderField) =>
    headerDrafts[f] ??
    (f === "subtotal" || f === "tax" || f === "total" ? editableNumber(invoice[f], "money") : (invoice[f] ?? ""));
  const moneyInput = "input num w-24 px-1.5 py-0.5 text-right";

  return (
    <div>
      <div className="mb-1 flex items-baseline gap-3">
        <h1 className="page-title">{invoice.invoice_number ?? invoice.id}</h1>
        <StatusBadge status={invoice.status} />
      </div>
      <p className="mb-4 text-sm text-gray-500">
        {invoice.distributor_name ?? "Unrecognized distributor"} ·{" "}
        {invoice.invoice_date ?? "no date"}
      </p>

      {editable && (
        <div
          className={`mb-4 rounded-lg border border-l-4 px-4 py-3 text-sm ${
            invoice.check.passes
              ? "border-brand-200 border-l-brand-400 bg-brand-50"
              : "border-amber-200 border-l-amber-400 bg-amber-50"
          }`}
        >
          <p className={`font-medium ${invoice.check.passes ? "text-brand-900" : "text-amber-900"}`}>
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
        <p className="mb-4 rounded-lg border border-brand-200 border-l-4 border-l-brand-400 bg-brand-50 px-4 py-2.5 text-sm font-medium text-brand-900">
          Confirmed after review. Its prices now feed your benchmarks, alerts
          and negotiation sheet.
        </p>
      )}

      {/* Side by side where there's room; the page image above the lines on
          smaller screens (a fixed 288px column beside the table pushed a
          phone-width page 250px past the screen). */}
      <div className="flex flex-col gap-6 xl:flex-row">
        {/* Only when there's an image: an empty 288px column beside the
            table clipped its last columns at laptop widths. */}
        {invoice.page_image_urls.length > 0 && (
          <div className="w-full max-w-sm space-y-3 xl:w-72 xl:flex-none">
            {invoice.page_image_urls.map((url) => (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  key={url}
                  src={`/api${url}`}
                  alt="Invoice page"
                  className="w-full rounded-lg border border-gray-200 shadow-sm"
                />
              ))}
          </div>
        )}

        <div className="min-w-0 flex-1">
          {editable && (
            <div className="mb-3 flex flex-wrap gap-4 text-sm">
              <label className="flex items-center gap-2">
                Distributor
                <select
                  value={
                    headerDrafts.distributor_id ??
                    (recognized ? (invoice.distributor_id ?? "") : "")
                  }
                  onChange={(e) =>
                    setHeaderDrafts((d) => ({
                      ...d,
                      distributor_id: e.target.value,
                    }))
                  }
                  className="input py-1.5"
                >
                  {!recognized && (
                    <option value="">Unrecognized — choose</option>
                  )}
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
                  onChange={(e) =>
                    setHeaderDrafts((d) => ({
                      ...d,
                      invoice_date: e.target.value,
                    }))
                  }
                  className="input py-1.5"
                />
              </label>
            </div>
          )}

          {invoice.line_items.length > 0 && (
          <div className="card overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead className="bg-gray-50">
                <tr className="border-b border-gray-200">
                  <th className="th pl-3">#</th>
                  <th className="th">Description</th>
                  <th className="th text-right">Qty</th>
                  <th className="th">UOM</th>
                  <th className="th text-right">Unit price</th>
                  <th className="th text-right">Extended</th>
                  <th className="th">Match</th>
                  {editable && <th className="th" />}
                </tr>
              </thead>
              <tbody>
                {invoice.line_items.map((li) => {
                  const flagged =
                    failed.has(li.line_number) &&
                    !edits.lines.some((e) => e.id === li.id);
                  return (
                    <tr
                      key={li.id}
                      className={`border-b border-gray-100 ${flagged ? "bg-red-50" : ""}`}
                    >
                      <td className="py-2 pl-3 pr-3 text-gray-500">{li.line_number}</td>
                      <td className="py-1.5 pr-3">
                        {editable ? (
                          <>
                            <input
                              aria-label={`Line ${li.line_number} description`}
                              title={lineValue(li, "raw_description")}
                              value={lineValue(li, "raw_description")}
                              onChange={setLine(li, "raw_description")}
                              className={`${textInput} w-full`}
                            />
                            <div className="mt-1 flex gap-1 text-xs">
                              <input
                                aria-label={`Line ${li.line_number} item code`}
                                placeholder="item code"
                                value={lineValue(li, "raw_sku")}
                                onChange={setLine(li, "raw_sku")}
                                className={`${textInput} w-20`}
                              />
                              <input
                                aria-label={`Line ${li.line_number} pack size`}
                                title={lineValue(li, "raw_pack_size")}
                                placeholder="pack size"
                                value={lineValue(li, "raw_pack_size")}
                                onChange={setLine(li, "raw_pack_size")}
                                className={`${textInput} w-24`}
                              />
                            </div>
                          </>
                        ) : (
                          <>
                            {li.raw_description}
                            <div className="text-xs text-gray-400">
                              {li.raw_sku ?? "no item code"} ·{" "}
                              {li.raw_pack_size ?? "no pack size"}
                            </div>
                          </>
                        )}
                        {flagged && (
                          <div className="mt-0.5 text-xs font-semibold text-red-700">
                            qty × unit price ≠ extended
                          </div>
                        )}
                      </td>
                      {(["quantity"] as LineField[]).map((f) => (
                        <td key={f} className="num py-1.5 pr-3 text-right">
                          {editable ? (
                            <input
                              aria-label={`Line ${li.line_number} ${f}`}
                              inputMode="decimal"
                              value={lineValue(li, f)}
                              onChange={(e) =>
                                setLineDrafts((d) => ({
                                  ...d,
                                  [li.id]: { ...d[li.id], [f]: e.target.value },
                                }))
                              }
                              className={moneyInput}
                            />
                          ) : (
                            quantity(li[f])
                          )}
                        </td>
                      ))}
                      <td className="py-1.5 pr-3">
                        {editable ? (
                          <input
                            aria-label={`Line ${li.line_number} unit`}
                            value={lineValue(li, "uom")}
                            onChange={setLine(li, "uom")}
                            className={`${textInput} w-12`}
                          />
                        ) : (
                          li.uom
                        )}
                      </td>
                      {(["unit_price", "extended_price"] as LineField[]).map(
                        (f) => (
                          <td key={f} className="num py-1.5 pr-3 text-right">
                            {editable ? (
                              <input
                                aria-label={`Line ${li.line_number} ${f.replace("_", " ")}`}
                                inputMode="decimal"
                                value={lineValue(li, f)}
                                onChange={(e) =>
                                  setLineDrafts((d) => ({
                                    ...d,
                                    [li.id]: {
                                      ...d[li.id],
                                      [f]: e.target.value,
                                    },
                                  }))
                                }
                                className={moneyInput}
                              />
                            ) : f === "unit_price" ? (
                              unitPrice(li[f])
                            ) : (
                              <span className="font-medium">{money(li[f])}</span>
                            )}
                          </td>
                        ),
                      )}
                      <td className="py-1.5 pr-3">
                        <span className={`badge ${REVIEW_BADGE[li.review_status] ?? "bg-gray-100 text-gray-700"}`}>
                          {li.review_status}
                        </span>
                      </td>
                      {editable && (
                        <td className="py-1.5 text-right">
                          <button
                            onClick={() => void removeLine(li)}
                            disabled={busy}
                            aria-label={`Remove line ${li.line_number}`}
                            className="text-xs font-medium text-red-600 hover:text-red-800 hover:underline disabled:opacity-50"
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
          </div>
          )}
          {/* No header row over nothing: one box that says what to do. */}
          {invoice.line_items.length === 0 && (
            <p className="rounded-lg border-2 border-dashed border-gray-300 bg-white px-4 py-6 text-center text-sm text-gray-500">
              No line items were detected on this invoice.
              {editable && " Add them from the invoice image."}
            </p>
          )}
          {editable && (
            <button
              onClick={() => setAdding(true)}
              disabled={busy}
              className="btn-secondary btn-sm mt-3 text-brand-800"
            >
              + Add line item
            </button>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-5 text-sm text-gray-700">
            {(["subtotal", "tax", "total"] as HeaderField[]).map((f) => (
              <label key={f} className="flex items-center gap-2 capitalize">
                {f}
                {editable ? (
                  <input
                    aria-label={f}
                    inputMode="decimal"
                    value={headerValue(f)}
                    onChange={(e) =>
                      setHeaderDrafts((d) => ({ ...d, [f]: e.target.value }))
                    }
                    className={moneyInput}
                  />
                ) : (
                  <span className={`num ${f === "total" ? "text-base font-semibold text-gray-900" : "font-medium"}`}>
                    {money(invoice[f])}
                  </span>
                )}
              </label>
            ))}
          </div>

          {editable && (
            <div className="mt-4 flex items-center gap-3">
              <button
                onClick={save}
                disabled={busy || !dirty}
                className="btn-secondary"
              >
                Save &amp; re-check
              </button>
              <button
                onClick={() => void send("POST", "/confirm")}
                disabled={busy || dirty || !invoice.check.passes}
                title={
                  dirty
                    ? "Save your changes first"
                    : !invoice.check.passes
                      ? "The numbers still don't add up"
                      : ""
                }
                className="btn-primary"
              >
                <span aria-hidden>✓</span> Confirm invoice
              </button>
              {dirty && (
                <span className="badge bg-amber-100 text-amber-800">Unsaved changes</span>
              )}
            </div>
          )}
          {error && (
            <p className="mt-3 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</p>
          )}
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
            router.refresh();
          }}
        />
      )}
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge ${STATUS_BADGE[status] ?? "bg-gray-100 text-gray-700"}`}>
      {STATUS_LABEL[status] ?? status.replace("_", " ")}
    </span>
  );
}
