/** How numbers look and what color they are, in one place.
 *
 * The API sends money as exact decimal strings with four places ("4240.1143",
 * six for a price per unit)
 * because that's what's stored; screens used to print them as is. These turn
 * them into what a person reads, and say which way a change is good: for
 * someone buying food, a price going up is bad (red) and down is good (green).
 */

type Num = string | number | null | undefined;

function toNumber(value: Num): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

const DASH = "—";

/** Totals and savings: always cents, with thousands separators. "$4,240.11" */
export function money(value: Num): string {
  const n = toNumber(value);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Unit prices, where fractions of a cent matter on small items: cents from
 *  $1 up, up to four places below it and six below a cent (one napkin of a
 *  3,000 case), trailing zeros dropped past the cent.
 *  "$18.06", "$0.6656", "$0.50", "$0.004167" */
export function unitPrice(value: Num): string {
  const n = toNumber(value);
  if (n === null) return DASH;
  const places = Math.abs(n) >= 1 ? 2 : Math.abs(n) >= 0.01 ? 4 : 6;
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: places });
}

/** Quantities: no padding zeros. "1,250", "2.5" */
export function quantity(value: Num): string {
  const n = toNumber(value);
  if (n === null) return DASH;
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

/** A fraction as a percentage. signed: a leading + on increases. "+24.2%" */
export function percent(fraction: Num, { signed = false, places = 1 } = {}): string {
  const n = toNumber(fraction);
  if (n === null) return DASH;
  const text = `${(n * 100).toFixed(places)}%`;
  return signed && n > 0 ? `+${text}` : text;
}

export type Tone = "bad" | "good" | "neutral";

/** Which way a price change reads for a buyer. */
export function priceChangeTone(change: Num): Tone {
  const n = toNumber(change);
  if (n === null || n === 0) return "neutral";
  return n > 0 ? "bad" : "good";
}

export const TONE_TEXT: Record<Tone, string> = {
  bad: "text-red-600",
  good: "text-brand-700",
  neutral: "text-gray-600",
};

/** Invoice statuses, as badge colors: done is green, needing a person amber,
 *  broken red, in progress blue. */
export const STATUS_BADGE: Record<string, string> = {
  extracted: "bg-brand-100 text-brand-800",
  confirmed: "bg-brand-100 text-brand-800",
  needs_review: "bg-amber-100 text-amber-800",
  failed: "bg-red-100 text-red-700",
  received: "bg-sky-100 text-sky-800",
  rendering: "bg-sky-100 text-sky-800",
  extracting: "bg-sky-100 text-sky-800",
};

/** A line's match: settled lines green, lines waiting on a person amber. */
export const REVIEW_BADGE: Record<string, string> = {
  auto: "bg-brand-100 text-brand-800",
  confirmed: "bg-brand-100 text-brand-800",
  corrected: "bg-sky-100 text-sky-800",
  pending: "bg-amber-100 text-amber-800",
  not_product: "bg-gray-100 text-gray-700",
};

/** Invoice statuses in plain words. The three steps of reading an invoice
 *  are one thing to the person waiting for it. */
export const STATUS_LABEL: Record<string, string> = {
  received: "Reading",
  rendering: "Reading",
  extracting: "Reading",
  extracted: "Ready",
  needs_review: "Needs a look",
  confirmed: "Confirmed",
  failed: "Couldn't read",
};

/** Still being read: the invoice has no numbers yet. */
export const READING_STATUSES = new Set(["received", "rendering", "extracting"]);

/** Whether a line has been matched to a product, in plain words. */
export const MATCH_LABEL: Record<string, string> = {
  auto: "Matched",
  pending: "To match",
  confirmed: "Confirmed",
  corrected: "Fixed",
  not_product: "Fee or charge",
};

/** How an invoice arrived. */
export const SOURCE_LABEL: Record<string, string> = {
  upload: "Upload",
  email: "Email",
  photo: "Photo",
  typed: "Typed in",
};

/** A stored decimal as the starting text of an edit box: no padding zeros
 *  and no separators (it has to parse back). Money keeps its cents ("74.50",
 *  "0.00") and any sub-cent places that carry information ("0.6656");
 *  quantities keep only what's there ("2", "2.5"). The value itself is
 *  unchanged: edits are compared as numbers. */
export function editableNumber(value: string | null | undefined, kind: "money" | "quantity"): string {
  if (value === null || value === undefined) return "";
  const text = String(value).trim();
  if (!/^-?\d+(\.\d+)?$/.test(text)) return text;
  const [whole, fraction = ""] = text.split(".");
  const trimmed = fraction.replace(/0+$/, "");
  if (kind === "quantity") return trimmed ? `${whole}.${trimmed}` : whole;
  return `${whole}.${trimmed.padEnd(2, "0")}`;
}
