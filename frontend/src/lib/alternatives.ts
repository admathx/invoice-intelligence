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
};

/** Whose price it is, in words: the reader's own, from their invoices, or
 *  what other businesses typically pay. The two mean different things (one
 *  is a price they have, the other a price they'd have to ask for), so the
 *  line always says which. */
export function alternativeSource(a: Alternative): string {
  if (a.yours) return a.last_bought ? `what you pay there now, last on ${a.last_bought}` : "what you pay there now";
  const where = a.scope === "metro" ? "in your area" : "nationwide";
  return `what ${a.distinct_account_count ?? "other"} similar businesses ${where} typically pay there`;
}
