import { describe, expect, it } from "vitest";

import {
  byCategory,
  byDistributor,
  categoryLabel,
  days,
  dollars,
  planned,
  recentAsSliders,
  signedDollars,
  signedPercent,
  type ProductCost,
} from "./costs";

const product = (name: string, category: string, distributor: string, cost: string, change: string | null): ProductCost => ({
  canonical_sku_id: name,
  name,
  category,
  distributor,
  yearly_cost: cost,
  recent_change: change,
});

const PRODUCTS = [
  product("Beef", "proteins", "Sysco", "9000.00", "0.1000"),
  product("Chicken", "proteins", "US Foods", "1000.00", null),
  product("Milk", "dairy", "Sysco", "5000.00", "-0.0200"),
];

describe("the year, grouped", () => {
  it("adds products up by category, dearest first, with each one's share", () => {
    const [proteins, dairy] = byCategory(PRODUCTS);
    expect([proteins.name, proteins.yearlyCost, proteins.products]).toEqual(["proteins", 10000, 2]);
    expect(proteins.share).toBeCloseTo(10000 / 15000);
    expect([dairy.name, dairy.yearlyCost]).toEqual(["dairy", 5000]);
  });

  it("weighs a category's recent move by what each product costs", () => {
    const [proteins, dairy] = byCategory(PRODUCTS);
    // Beef, 90% of proteins, rose 10%; chicken was bought once, so it shows no move.
    expect(proteins.recentChange).toBeCloseTo(0.09);
    expect(dairy.recentChange).toBeCloseTo(-0.02);
  });

  it("adds the same products up by distributor", () => {
    expect(byDistributor(PRODUCTS).map((g) => [g.name, g.yearlyCost])).toEqual([
      ["Sysco", 14000],
      ["US Foods", 1000],
    ]);
  });

  it("has nothing to say about no products", () => {
    expect(byCategory([])).toEqual([]);
  });
});

describe("a plan", () => {
  const groups = byCategory(PRODUCTS);

  it("costs the same as now when nothing is moved", () => {
    expect(planned(groups, {}, 0)).toMatchObject({ now: 15000, then: 15000, change: 0 });
  });

  it("moves one category's prices and leaves the rest", () => {
    const plan = planned(groups, { proteins: 10 }, 0);
    expect(plan.rows.map((r) => Math.round(r.change))).toEqual([1000, 0]);
    expect(Math.round(plan.then)).toBe(16000);
  });

  it("multiplies a price change by a volume change", () => {
    // 10% more of something 10% dearer is 21% more money; the rest is 10% more.
    const plan = planned(groups, { proteins: 10 }, 10);
    expect(Math.round(plan.then)).toBe(12100 + 5500);
    expect(Math.round(plan.change)).toBe(2600);
  });

  it("can save: prices down, or buying less", () => {
    expect(Math.round(planned(groups, { dairy: -10 }, 0).change)).toBe(-500);
    expect(Math.round(planned(groups, {}, -20).change)).toBe(-3000);
  });

  it("starts the sliders from each category's recent move, to the half point and within range", () => {
    expect(recentAsSliders(groups, -20, 30)).toEqual({ proteins: 9, dairy: -2 });
    expect(recentAsSliders([{ ...groups[0], recentChange: 0.0076 }], -20, 30)).toEqual({ proteins: 1 });
    expect(recentAsSliders([{ ...groups[0], recentChange: 0.5 }], -20, 30)).toEqual({ proteins: 30 });
  });
});

describe("how the figures are said", () => {
  it("names categories as headings", () => {
    expect(categoryLabel("proteins")).toBe("Proteins");
    expect(categoryLabel("")).toBe("Other");
  });

  it("gives yearly figures in whole dollars, and changes with their sign", () => {
    expect(dollars(498494.25)).toBe("$498,494");
    expect(signedDollars(3231.22)).toBe("+$3,231");
    expect(signedDollars(-2714.76)).toBe("−$2,715");
    expect(signedDollars(0.3)).toBe("$0");
  });

  it("counts days in the singular when there's one", () => {
    expect(days(1)).toBe("1 day");
    expect(days(85)).toBe("85 days");
  });

  it("gives percentages with their sign", () => {
    expect(signedPercent(5)).toBe("+5%");
    expect(signedPercent(-0.5)).toBe("−0.5%");
    expect(signedPercent(0)).toBe("0%");
  });
});
