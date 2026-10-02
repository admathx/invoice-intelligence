"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { useLocationId } from "@/components/SessionContext";
import { api } from "@/lib/api";
import { formatApiError } from "@/lib/apiError";
import ActionButton from "@/components/ActionButton";
import DistributorPicker, { withDistributor, type Distributor } from "@/components/DistributorPicker";
import RefreshWhileReading from "@/components/RefreshWhileReading";
import {
  editableNumber,
  MATCH_LABEL,
  money,
  quantity,
  READING_STATUSES,
  REVIEW_BADGE,
  STATUS_BADGE,
  STATUS_LABEL,
  unitPrice,
} from "@/lib/format";

import AddLineModal from "./AddLineModal";

type LineItem = {
  id: string;
  line_number: number;
  raw_description: string;
  raw_sku: string | null;
  raw_pack_size: string | null;
  // A pack a person entered for this item, not printed on the page.
  pack_size_remembered: boolean;
  // What the invoice printed, when an entered or remembered pack replaced it.
  printed_pack_size: string | null;
  quantity: string;
  unit_price: string;
  extended_price: string;
  uom: string;
  normalized_unit_price: string | null;
  base_uom: string | null;
  canonical_sku_id: string | null;
  canonical_sku_name: string | null;
  review_status: string;
};

export type InvoiceDetail = {
  id: string;
  invoice_number: string | null;
  invoice_date: string | null;
  distributor_id: string | null;
  distributor_name: string | null;
  status: string;
  // "typed": a person is typing it in, and there is no page image.
  source: string;
  subtotal: string | null;
  tax: string | null;
  total: string | null;
  line_items: LineItem[];
  page_image_urls: string[];
  check: { passes: boolean; reasons: string[]; failed_line_numbers: number[] };
  // Held whatever the numbers say: a likely copy of another invoice, or not
  // an invoice at all (a statement, a price list).
  duplicate_of_id: string | null;
  duplicate_of_label: string | null;
  duplicate_is_same_file: boolean;
  duplicate_is_reissue: boolean;
  document_type: string | null;
  // The distributor's name as printed, offered when adding one.
  printed_distributor: string | null;
  // Also held: made out to what looks like another restaurant.
  printed_customer: string | null;
  billed_elsewhere: boolean;
  // Said when the file held more than one invoice.
  split_note: string | null;
  shares_page: boolean;
};

const DOCUMENT_LABEL: Record<string, string> = {
  statement: "a statement",
  price_list: "a price list or order guide",
};

export type { Distributor };

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

const HEADER_LABEL: Record<string, string> = { subtotal: "Subtotal", tax: "Tax", total: "Total" };

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
  "subtotal" | "tax" | "total" | "invoice_date" | "invoice_number" | "distributor_id";

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
  // Whenever the page refreshes, show the server's latest: it finished being
  // read, someone else confirmed it, an item was sent back to be matched.
  // Anything typed and not yet saved stays: drafts are kept separately, by
  // line and field, and only replace what they were typed over.
  useEffect(() => setInvoice(initial), [initial]);
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
  // Plus any added here, so a new one can be chosen straight away.
  const [distributorList, setDistributorList] = useState(distributors);
  useEffect(() => setDistributorList(distributors), [distributors]);
  // Extraction's "other" is stored as a real distributor row but isn't in the
  // picker (it isn't a choice), so it reads as unrecognized here, same as NULL.
  const recognized = distributorList.some((d) => d.id === invoice.distributor_id);
  const held = invoice.duplicate_of_id
    ? "copy"
    : invoice.document_type
      ? "document"
      : invoice.billed_elsewhere
        ? "elsewhere"
        : null;
  const reading = READING_STATUSES.has(invoice.status);
  // Typed in, so what it's checked against is the person's own paper.
  const typed = invoice.source === "typed";
  const paper = typed ? "your invoice" : "the picture";

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
      // The number can be cleared (some invoices print none); a date or a
      // distributor can only be replaced.
      if (f === "invoice_number") return value.trim() !== (invoice.invoice_number ?? "");
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
            "That didn't save. Check the numbers and try again.",
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
      !window.confirm(`Remove item ${li.line_number} (${li.raw_description})?`)
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
        setError(formatApiError(data?.detail, "Couldn't remove that item."));
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

  async function deleteInvoice() {
    const what = held === "copy" ? "this copy" : "this invoice";
    if (!window.confirm(`Delete ${what}? Its prices come out of your price history too. This can't be undone.`)) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(`/invoices/${invoice.id}?tenant_id=${locationId}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(formatApiError(data?.detail, "Couldn't delete it."));
        return;
      }
      router.push("/invoices");
      router.refresh();
    } catch {
      setError("Couldn't reach the server. Nothing was deleted.");
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
      <a href="/invoices" className="link mb-2 inline-block text-sm">
        &larr; All invoices
      </a>
      <div className="mb-1 flex flex-wrap items-baseline gap-3">
        <h1 className="page-title">{invoice.invoice_number ? `Invoice ${invoice.invoice_number}` : "Invoice"}</h1>
        <StatusBadge status={invoice.status} />
      </div>
      <p className="mb-4 text-sm text-gray-500">
        {invoice.distributor_name ?? "Distributor not known"} ·{" "}
        {invoice.invoice_date ?? "No date"}
      </p>

      {reading && (
        <p role="status" className="mb-4 rounded-lg border border-sky-200 border-l-4 border-l-sky-400 bg-sky-50 px-4 py-2.5 text-sm font-medium text-sky-900">
          We&rsquo;re still reading this invoice. This page updates by itself when it&rsquo;s done.
          <RefreshWhileReading reading />
        </p>
      )}

      {invoice.split_note && (
        <p className={`mb-3 text-sm ${invoice.shares_page && editable ? "font-medium text-amber-800" : "text-gray-600"}`}>
          {invoice.split_note}
        </p>
      )}

      {editable && (
        <div
          className={`mb-4 rounded-lg border border-l-4 px-4 py-3 text-sm ${
            invoice.check.passes
              ? "border-brand-200 border-l-brand-400 bg-brand-50"
              : "border-amber-200 border-l-amber-400 bg-amber-50"
          }`}
        >
          <p className={`font-medium ${invoice.check.passes ? "text-brand-900" : "text-amber-900"}`}>
            {held === "copy" && invoice.duplicate_is_same_file
              ? `These look like more pages of ${invoice.duplicate_of_label ?? "an invoice"} from the same file, so nothing here is used. Delete this and add anything missing to that invoice.`
              : held === "copy" && invoice.duplicate_is_reissue
              ? `This and ${invoice.duplicate_of_label ?? "an invoice you already added"} look like an original and its reissue, so nothing on this one is used until one of the two is deleted. Delete this one, or open that one and delete it: this then takes its place.`
              : held === "copy"
              ? `This looks like a copy of ${invoice.duplicate_of_label ?? "an invoice you already added"}, so nothing on it is used.`
              : held === "document"
                ? `This looks like ${DOCUMENT_LABEL[invoice.document_type ?? ""] ?? "something other than an invoice"}, so nothing on it is used.`
                : held === "elsewhere"
                  ? `This is made out to ${invoice.printed_customer ?? "another restaurant"}, which doesn't look like this restaurant, so nothing on it is used.`
                  : invoice.status === "failed"
                  ? "We couldn't read this invoice. Type in its items from the picture, then save."
                  : invoice.check.passes && invoice.shares_page
                    ? "This one adds up. Add the other invoice on its page, then confirm this one to start using its prices."
                  : invoice.check.passes
                    ? "Everything adds up now. Confirm the invoice to start using its prices."
                    : typed
                      ? "You're typing this invoice in. Its prices are used once it adds up and you confirm it."
                      : "Some numbers on this invoice don't add up, so its prices aren't being used yet."}
          </p>
          {held && (
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" onClick={() => void deleteInvoice()} disabled={busy} className="btn-primary btn-sm">
                Delete it
              </button>
              <button type="button" onClick={() => void send("POST", "/keep")} disabled={busy} className="btn-secondary btn-sm">
                {held === "copy"
                  ? invoice.duplicate_is_reissue
                    ? "They're separate invoices"
                    : "It's a different invoice"
                  : held === "elsewhere"
                    ? "It's ours"
                    : "It is an invoice"}
              </button>
            </div>
          )}
          {!held && !invoice.check.passes && (
            <>
              <ul className="mt-1 list-disc pl-5 text-amber-800">
                {invoice.check.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
              <p className="mt-2 text-amber-800">
                {typed
                  ? // No page to check the typing against, so the printed
                    // amounts are: a slip in one shows as not adding up.
                    "Add each item, then the total. Type every line total and the total as printed, not worked out: that is how a slip in the typing gets caught."
                  : invoice.line_items.length === 0
                    ? "Add each item from the picture, fill in the totals, then save."
                    : "Check the highlighted items against the picture, fix anything that was misread, then save."}
              </p>
            </>
          )}
        </div>
      )}
      {invoice.status === "confirmed" && (
        <p className="mb-4 rounded-lg border border-brand-200 border-l-4 border-l-brand-400 bg-brand-50 px-4 py-2.5 text-sm font-medium text-brand-900">
          Confirmed. Its prices now count toward your price alerts and savings.
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
                  alt="The invoice"
                  className="w-full rounded-lg border border-gray-200 shadow-sm"
                />
              ))}
          </div>
        )}

        <div className="min-w-0 flex-1">
          {editable && (
            <div className="mb-3 flex flex-wrap gap-4 text-sm">
              <DistributorPicker
                distributors={distributorList}
                value={headerDrafts.distributor_id ?? (recognized ? (invoice.distributor_id ?? "") : "")}
                onChange={(id) => setHeaderDrafts((d) => ({ ...d, distributor_id: id }))}
                onAdded={(added) => setDistributorList((list) => withDistributor(list, added))}
                suggestedName={initial.printed_distributor ?? ""}
                // The name is what we read off the page, which on a crumpled
                // or faxed invoice can be wrong.
                addHint={`${typed ? "" : "Check the name against the picture. "}If it's one already in the list, choose that instead.`}
                disabled={busy}
              />
              {!recognized && invoice.printed_distributor && (
                <span className="self-center text-xs text-gray-500">
                  The invoice says &ldquo;{invoice.printed_distributor}&rdquo;.
                </span>
              )}
              <label className="flex items-center gap-2">
                Invoice #
                <input
                  value={headerValue("invoice_number")}
                  onChange={(e) => setHeaderDrafts((d) => ({ ...d, invoice_number: e.target.value }))}
                  maxLength={100}
                  className="input w-36 py-1.5"
                />
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
                  <th className="th">Item</th>
                  <th className="th text-right">Qty</th>
                  <th className="th">Unit</th>
                  <th className="th text-right">Price each</th>
                  <th className="th text-right">Line total</th>
                  <th className="th">Product</th>
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
                              {li.pack_size_remembered && (
                                <span title="Entered for this item before; not what this invoice printed"> (remembered)</span>
                              )}
                              {li.printed_pack_size && <> &middot; invoice says &ldquo;{li.printed_pack_size}&rdquo;</>}
                            </div>
                            {li.normalized_unit_price === null &&
                              !held &&
                              !reading &&
                              li.review_status !== "not_product" &&
                              li.review_status !== "pending" && <AddPackSize line={li} locationId={locationId} />}
                          </>
                        )}
                        {flagged && (
                          <div className="mt-0.5 text-xs font-semibold text-red-700">
                            Qty × price each doesn&rsquo;t equal the line total
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
                        {li.canonical_sku_id && li.canonical_sku_name && (
                          <a href={`/skus/${li.canonical_sku_id}`} className="link block max-w-[12rem] truncate text-xs" title={li.canonical_sku_name}>
                            {li.canonical_sku_name}
                          </a>
                        )}
                        <span className={`badge ${REVIEW_BADGE[li.review_status] ?? "bg-gray-100 text-gray-700"}`}>
                          {MATCH_LABEL[li.review_status] ?? li.review_status}
                        </span>
                        {li.review_status === "pending" && !held ? (
                          <a href="/review" className="link ml-1 whitespace-nowrap text-xs">
                            Match it
                          </a>
                        ) : li.review_status === "not_product" ? (
                          !editable && (
                            <div className="mt-0.5">
                              <ActionButton
                                label="It's a product"
                                question={`Is “${li.raw_description}” something you bought? It goes to Match items to be matched.`}
                                path={`/review/${li.id}/reopen?tenant_id=${locationId}`}
                              />
                            </div>
                          )
                        ) : (
                          !editable &&
                          li.canonical_sku_name && (
                            <div className="mt-0.5">
                              <ActionButton
                                label="Wrong product?"
                                question={`Is “${li.raw_description}” not ${li.canonical_sku_name}? It goes back to Match items to be matched again.`}
                                path={`/review/${li.id}/reopen?tenant_id=${locationId}`}
                              />
                            </div>
                          )
                        )}
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
              {typed ? "No items yet." : "We didn’t find any items on this invoice."}
              {editable && ` Add them from ${paper}.`}
            </p>
          )}
          {editable && (
            <button
              onClick={() => setAdding(true)}
              disabled={busy}
              className="btn-secondary btn-sm mt-3 text-brand-800"
            >
              + Add item
            </button>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-5 text-sm text-gray-700">
            {(["subtotal", "tax", "total"] as HeaderField[]).map((f) => (
              <label key={f} className="flex items-center gap-2">
                {HEADER_LABEL[f]}
                {editable ? (
                  <input
                    aria-label={HEADER_LABEL[f]}
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
                Save and check
              </button>
              <button
                onClick={() => void send("POST", "/confirm")}
                disabled={busy || dirty || !invoice.check.passes}
                title={
                  dirty
                    ? "Save your changes first"
                    : !invoice.check.passes
                      ? "Some numbers still don't add up"
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
          {!reading && !held && (
            <p className="mt-6 border-t border-gray-100 pt-3 text-xs text-gray-500">
              Added twice, or not an invoice?{" "}
              <button type="button" onClick={() => void deleteInvoice()} disabled={busy} className="font-medium text-red-600 hover:underline disabled:opacity-50">
                Delete this invoice
              </button>
            </p>
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

/** A settled line whose price per unit can't be worked out: enter its pack
 *  size once; it's remembered for the item and filled in elsewhere. */
function AddPackSize({ line, locationId }: { line: LineItem; locationId: string | null }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [pack, setPack] = useState(line.raw_pack_size ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!pack.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api(`/review/${line.id}/pack?tenant_id=${locationId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pack_size: pack }),
      });
      if (!res.ok) {
        setError(formatApiError((await res.json().catch(() => null))?.detail, "Couldn't save that."));
        return;
      }
      setOpen(false);
      router.refresh();
    } catch {
      setError("Couldn't reach the server. Nothing was saved.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className="mt-0.5 text-xs font-medium text-brand-700 hover:underline">
        Add pack size to track its price
      </button>
    );
  }
  return (
    <form onSubmit={save} className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
      <input
        value={pack}
        onChange={(e) => setPack(e.target.value)}
        placeholder="4/5 LB"
        aria-label={`Pack size for line ${line.line_number}`}
        className="input w-24 px-1.5 py-0.5"
        autoFocus
      />
      <button type="submit" disabled={busy || !pack.trim()} className="btn-secondary btn-sm">
        Save
      </button>
      <button type="button" onClick={() => setOpen(false)} className="text-gray-600 hover:underline">
        Cancel
      </button>
      {error && <span className="basis-full text-red-700">{error}</span>}
    </form>
  );
}
