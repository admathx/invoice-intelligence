import { describe, expect, it } from "vitest";

import { compactMoney, monthLabel, niceCeiling, periodMonths, summarize, type MonthSpend } from "./periods";

const month = (m: string, total: number, cats: Record<string, number> = {}): MonthSpend => ({
  month: m,
  total: String(total),
  invoice_count: 1,
  by_category: Object.fromEntries(Object.entries(cats).map(([k, v]) => [k, String(v)])),
  by_distributor: { Sysco: String(total) },
});

// June, July, August, and September (the current month).
const MONTHS = [
  month("2026-06", 300, { Produce: 100, Proteins: 200 }),
  month("2026-07", 400, { Produce: 100, Proteins: 300 }),
  month("2026-08", 500, { Produce: 150, Proteins: 300, Dairy: 50 }),
  month("2026-09", 50, { Produce: 50 }),
];

describe("periods", () => {
  it("picks the months and the stretch before them", () => {
    expect(periodMonths(MONTHS, { kind: "this-month" })).toEqual({ now: [3], before: [2] });
    expect(periodMonths(MONTHS, { kind: "last-month" })).toEqual({ now: [2], before: [1] });
    expect(periodMonths(MONTHS, { kind: "last-3" })).toEqual({ now: [0, 1, 2], before: [] });
    expect(periodMonths(MONTHS, { kind: "month", month: "2026-07" })).toEqual({ now: [1], before: [0] });
  });

  it("sums a month, compares it with the one before, and sorts biggest first", () => {
    const s = summarize(MONTHS, { kind: "last-month" });
    expect(s.label).toBe("August 2026");
    expect(s.previousLabel).toBe("Jul");
    expect(s.total).toBe(500);
    expect(s.previousTotal).toBe(400);
    expect(s.categories.map((c) => c.name)).toEqual(["Proteins", "Produce", "Dairy"]);
    expect(s.categories[1]).toMatchObject({ amount: 150, share: 0.3, change: 0.5 });
    expect(s.categories[2].change).toBeNull(); // nothing to compare with
  });

  it("makes no comparison without a full earlier stretch", () => {
    expect(summarize(MONTHS, { kind: "last-3" }).previousTotal).toBeNull();
  });
});

describe("chart helpers", () => {
  it("rounds the top of the chart up to a readable number", () => {
    expect(niceCeiling(45115)).toBe(50000);
    expect(niceCeiling(38000)).toBe(40000);
    expect(niceCeiling(0)).toBe(1);
  });

  it("writes axis money short", () => {
    expect(compactMoney(40000)).toBe("$40k");
    expect(compactMoney(12500)).toBe("$12.5k");
    expect(compactMoney(1_500_000)).toBe("$1.5M");
    expect(compactMoney(800)).toBe("$800");
  });

  it("names months", () => {
    expect(monthLabel("2026-08")).toBe("August 2026");
    expect(monthLabel("2026-08", "short")).toBe("Aug");
  });
});
