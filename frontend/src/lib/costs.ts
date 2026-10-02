/** A year of a location's buying at today's prices, and what would change
 *  it (backend app/analytics/costs.py). The backend sends each product's
 *  yearly cost; grouping them and moving the prices is done here, as the
 *  person drags a slider.
 *
 *  Numbers, not strings: unlike an invoice's amounts, these are estimates
 *  shown in whole dollars, and the arithmetic is the page's whole job. */
export type ProductCost = {
  canonical_sku_id: string;
  name: string;
  category: string;
  distributor: string;
  yearly_cost: string;
  // How its price moved over the window (0.05 is 5% up); null if bought once.
  recent_change: string | null;
};

export type ScenarioKey = "increases_reversed" | "cheaper_elsewhere" | "savings_targets" | "rise_again";

export type Scenario = { key: ScenarioKey; yearly_change: string; products: number };

export type Costs = {
  window_start: string | null;
  window_end: string | null;
  window_days: number;
  yearly_cost: string;
  coverage: string | null;
  products: ProductCost[];
  scenarios: Scenario[];
};

export type Group = {
  name: string;
  yearlyCost: number;
  products: number;
  // Of the whole year, 0..1.
  share: number;
  // How its prices moved over the window, weighted by what each product
  // costs: a 10% rise on beef counts for more than one on toothpicks.
  recentChange: number;
};

function grouped(products: ProductCost[], key: (p: ProductCost) => string): Group[] {
  const groups = new Map<string, { cost: number; moved: number; products: number }>();
  let total = 0;
  for (const p of products) {
    const cost = Number(p.yearly_cost) || 0;
    const g = groups.get(key(p)) ?? { cost: 0, moved: 0, products: 0 };
    g.cost += cost;
    g.moved += cost * (Number(p.recent_change) || 0);
    g.products += 1;
    groups.set(key(p), g);
    total += cost;
  }
  return [...groups.entries()]
    .map(([name, g]) => ({
      name,
      yearlyCost: g.cost,
      products: g.products,
      share: total > 0 ? g.cost / total : 0,
      recentChange: g.cost > 0 ? g.moved / g.cost : 0,
    }))
    .sort((a, b) => b.yearlyCost - a.yearlyCost || a.name.localeCompare(b.name));
}

/** The year by category, dearest first. */
export function byCategory(products: ProductCost[]): Group[] {
  return grouped(products, (p) => p.category);
}

/** The year by distributor, dearest first. */
export function byDistributor(products: ProductCost[]): Group[] {
  return grouped(products, (p) => p.distributor);
}

/** A catalog category as a heading: "proteins" is "Proteins". */
export function categoryLabel(category: string): string {
  const words = category.replaceAll("_", " ").trim();
  return words ? words[0].toUpperCase() + words.slice(1) : "Other";
}

export type Plan = {
  rows: { name: string; now: number; then: number; change: number }[];
  now: number;
  then: number;
  change: number;
};

/** What the year would cost with each category's prices moved by
 *  `priceChange[name]` percent and everything bought `volumeChange` percent
 *  more (or, negative, less). Both at once multiply: 10% more of something
 *  10% dearer is 21% more money. */
export function planned(groups: Group[], priceChange: Record<string, number>, volumeChange: number): Plan {
  const rows = groups.map((g) => {
    const then = g.yearlyCost * (1 + (priceChange[g.name] ?? 0) / 100) * (1 + volumeChange / 100);
    return { name: g.name, now: g.yearlyCost, then, change: then - g.yearlyCost };
  });
  const now = rows.reduce((sum, r) => sum + r.now, 0);
  const then = rows.reduce((sum, r) => sum + r.then, 0);
  return { rows, now, then, change: then - now };
}

/** Each category's recent move as a slider position: percent, to the
 *  slider's half-point, within its range. */
export function recentAsSliders(groups: Group[], min: number, max: number): Record<string, number> {
  return Object.fromEntries(
    groups.map((g) => [g.name, Math.min(max, Math.max(min, Math.round(g.recentChange * 200) / 2))]),
  );
}

/** A yearly figure: whole dollars, because it's an estimate. */
export function dollars(n: number): string {
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

/** A change to a yearly figure, signed, with a real minus: "+$3,231", "−$2,715", "$0". */
export function signedDollars(n: number): string {
  const rounded = Math.round(n);
  if (rounded === 0) return "$0";
  return `${rounded > 0 ? "+" : "−"}${dollars(Math.abs(rounded))}`;
}

/** A percentage change, signed: "+1.5%", "−0.5%", "0%". */
export function signedPercent(percent: number): string {
  if (percent === 0) return "0%";
  const text = Number.isInteger(percent) ? String(Math.abs(percent)) : Math.abs(percent).toFixed(1);
  return `${percent > 0 ? "+" : "−"}${text}%`;
}
