import { describe, expect, it } from "vitest";

import { editableNumber, money, percent, priceChangeTone, quantity, unitPrice } from "./format";

describe("format", () => {
  it("shows totals in dollars and cents with separators", () => {
    expect(money("4240.1143")).toBe("$4,240.11");
    expect(money(0)).toBe("$0.00");
    expect(money(null)).toBe("—");
    expect(money("not a number")).toBe("—");
  });

  it("keeps fractions of a cent only where they matter", () => {
    expect(unitPrice("18.0618")).toBe("$18.06");
    expect(unitPrice("0.6656")).toBe("$0.6656");
    expect(unitPrice("0.5000")).toBe("$0.50");
  });

  it("drops padding zeros from quantities", () => {
    expect(quantity("1250.0000")).toBe("1,250");
    expect(quantity("2.5000")).toBe("2.5");
  });

  it("signs increases when asked", () => {
    expect(percent("0.2423", { signed: true })).toBe("+24.2%");
    expect(percent("-0.05", { signed: true })).toBe("-5.0%");
    expect(percent("0.5")).toBe("50.0%");
  });

  it("starts edit boxes without padding zeros, keeping cents on money", () => {
    expect(editableNumber("74.5000", "money")).toBe("74.50");
    expect(editableNumber("0.6656", "money")).toBe("0.6656");
    expect(editableNumber("0.0000", "money")).toBe("0.00");
    expect(editableNumber("1234567.8900", "money")).toBe("1234567.89");
    expect(editableNumber("2.0000", "quantity")).toBe("2");
    expect(editableNumber("2.5000", "quantity")).toBe("2.5");
    expect(editableNumber(null, "money")).toBe("");
    expect(editableNumber("not a number", "money")).toBe("not a number");
  });

  it("reads a price change from the buyer's side", () => {
    expect(priceChangeTone("0.1")).toBe("bad");
    expect(priceChangeTone("-0.1")).toBe("good");
    expect(priceChangeTone(0)).toBe("neutral");
  });
});
