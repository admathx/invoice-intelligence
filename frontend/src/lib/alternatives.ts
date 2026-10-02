/** Another distributor a product on a price alert costs less at (backend
 *  app/analytics/alternatives.py). */
export type Alternative = {
  distributor_name: string;
  price: string;
  // How much less than the alert's current price, 0..1.
  saving_pct: string;
  // This location's own price there; otherwise what others typically pay.
  yours: boolean;
  last_bought: string | null;
  distinct_account_count: number | null;
  scope: string | null;
  // The distributor's own site.
  website: string | null;
  // What the difference comes to in a year at what this location buys.
  annual_saving: string | null;
  // Whether it's worth acting on, and what that would involve (backend
  // app/analytics/switching.py): a lower price alone isn't a saving for a
  // restaurant that gets its pricing by ordering mostly from one distributor.
  advice: { verdict: "move" | "negotiate" | "stay"; headline: string; points: string[] } | null;
};

/** The yearly figure as it's said: whole dollars, because it's an estimate. */
export function yearlySaving(a: Alternative): string | null {
  const n = a.annual_saving === null ? NaN : Number(a.annual_saving);
  if (!Number.isFinite(n) || n <= 0) return null;
  const amount =
    n < 1
      ? "under $1"
      : `about ${n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 })}`;
  // Others' price is one this location hasn't been offered yet.
  return `${amount} a year${a.yours ? "" : " at that price"}`;
}

/** The distributor's site, if it's a link worth following: https only. */
export function supplierLink(a: Alternative): string | null {
  return a.website?.startsWith("https://") ? a.website : null;
}

/** Whose price it is, in words: the reader's own, from their invoices, or
 *  what other businesses typically pay. The two mean different things (one
 *  is a price they have, the other a price they'd have to ask for), so the
 *  line always says which. */
export function alternativeSource(a: Alternative): string {
  if (a.yours) return a.last_bought ? `what you pay there now, last on ${a.last_bought}` : "what you pay there now";
  const where = a.scope === "metro" ? "in your area" : "nationwide";
  return `what ${a.distinct_account_count ?? "other"} similar businesses ${where} typically pay there`;
}
