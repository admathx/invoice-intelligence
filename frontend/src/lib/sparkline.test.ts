import { describe, expect, it } from "vitest";
import { computeSparklineCoords } from "./sparkline";

describe("computeSparklineCoords", () => {
  it("returns nothing for fewer than 2 points", () => {
    expect(computeSparklineCoords([], 100, 40)).toEqual([]);
    expect(computeSparklineCoords([{ observed_on: "2026-01-01", unit_price_base: "1.00" }], 100, 40)).toEqual([]);
  });

  it("maps the lowest price to the bottom and the highest to the top", () => {
    const points = [
      { observed_on: "2026-01-01", unit_price_base: "1.00" },
      { observed_on: "2026-01-08", unit_price_base: "3.00" },
      { observed_on: "2026-01-15", unit_price_base: "2.00" },
    ];
    const coords = computeSparklineCoords(points, 100, 40, 2);
    const ys = coords.map((c) => Number(c.split(",")[1]));

    // SVG y grows downward: the lowest price (index 0) should have the
    // largest y, the highest price (index 1) the smallest y.
    expect(ys[0]).toBeGreaterThan(ys[1]);
    expect(ys[2]).toBeGreaterThan(ys[1]);
    expect(ys[2]).toBeLessThan(ys[0]);
  });

  it("spaces x coordinates evenly across the width", () => {
    const points = Array.from({ length: 5 }, (_, i) => ({
      observed_on: `2026-01-${String(i + 1).padStart(2, "0")}`,
      unit_price_base: "1.00",
    }));
    const coords = computeSparklineCoords(points, 100, 40, 0);
    const xs = coords.map((c) => Number(c.split(",")[0]));
    expect(xs).toEqual([0, 25, 50, 75, 100]);
  });

  it("does not divide by zero when every price is identical", () => {
    const points = [
      { observed_on: "2026-01-01", unit_price_base: "5.00" },
      { observed_on: "2026-01-08", unit_price_base: "5.00" },
    ];
    const coords = computeSparklineCoords(points, 100, 40);
    expect(coords.every((c) => !c.includes("NaN") && !c.includes("Infinity"))).toBe(true);
  });
});
