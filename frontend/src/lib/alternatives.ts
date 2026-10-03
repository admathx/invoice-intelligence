import { dollars } from "./costs";

/** Another distributor a product on a price alert costs less at (backend
 *  app/analytics/alternatives.py), and whether that's worth acting on
 *  (app/analytics/switching.py): a lower price alone isn't a saving for a
 *  restaurant that gets its pricing by buying mostly from one distributor. */
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
  advice: {
    verdict: "move" | "negotiate" | "stay";
    // What to do, in a few words: "Ask Sysco to match".
    action: string;
    headline: string;
    points: string[];
  } | null;
};

/** Whose price it is, in a few words: the reader's own, from their invoices,
 *  or what other businesses pay. One is a price they have, the other a price
 *  they'd have to ask for, so it always says which. */
export function alternativeSource(a: Alternative): string {
  if (a.yours) return a.last_bought ? `your price there, last paid ${a.last_bought}` : "your price there";
  const where = a.scope === "metro" ? "near you" : "nationwide";
  return `what ${a.distinct_account_count ?? "other"} businesses ${where} pay`;
}

/** The yearly figure as it's said: whole dollars, because it's an estimate. */
export function yearlySaving(a: Alternative): string | null {
  const n = a.annual_saving === null ? NaN : Number(a.annual_saving);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n < 1 ? "under $1" : `about ${dollars(n)}`;
}

/** The distributor's site, if it's a link worth following: https only. */
export function supplierLink(a: Alternative): string | null {
  return a.website?.startsWith("https://") ? a.website : null;
}

/** Where the next step is taken. Moving a product is done at the other
 *  distributor's site; asking the current one to match, or staying, is done
 *  with the numbers on Savings. */
export function nextStepLink(a: Alternative): string | null {
  if (!a.advice) return null;
  return a.advice.verdict === "move" ? supplierLink(a) : "/negotiation";
}
