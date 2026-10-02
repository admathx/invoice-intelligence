import { describe, expect, it } from "vitest";

import { alternativeSource, supplierLink, yearlySaving, type Alternative } from "./alternatives";

const base: Alternative = {
  distributor_name: "US Foods",
  price: "8.500000",
  saving_pct: "0.1500",
  yours: true,
  last_bought: "2026-09-12",
  distinct_account_count: null,
  scope: null,
  website: "https://www.usfoods.com",
  annual_saving: "525.00",
  advice: null,
};

describe("where a cheaper price comes from", () => {
  it("says a price is the reader's own, and when they last paid it", () => {
    expect(alternativeSource(base)).toBe("what you pay there now, last on 2026-09-12");
    expect(alternativeSource({ ...base, last_bought: null })).toBe("what you pay there now");
  });

  it("says a price is other businesses', how many, and where", () => {
    const others = { ...base, yours: false, last_bought: null, distinct_account_count: 7 };
    expect(alternativeSource({ ...others, scope: "metro" })).toBe(
      "what 7 similar businesses in your area typically pay there",
    );
    expect(alternativeSource({ ...others, scope: "national" })).toBe(
      "what 7 similar businesses nationwide typically pay there",
    );
  });
});

describe("what an alternative is worth, and where to find it", () => {
  it("gives the yearly saving in whole dollars, and says when the price is someone else's", () => {
    expect(yearlySaving(base)).toBe("about $525 a year");
    expect(yearlySaving({ ...base, yours: false, annual_saving: "1311.40" })).toBe("about $1,311 a year at that price");
    expect(yearlySaving({ ...base, annual_saving: "0.40" })).toBe("under $1 a year");
  });

  it("says nothing when there is no figure to give", () => {
    expect(yearlySaving({ ...base, annual_saving: null })).toBeNull();
    expect(yearlySaving({ ...base, annual_saving: "0.00" })).toBeNull();
  });

  it("links only to a secure address", () => {
    expect(supplierLink(base)).toBe("https://www.usfoods.com");
    expect(supplierLink({ ...base, website: null })).toBeNull();
    expect(supplierLink({ ...base, website: "javascript:alert(1)" })).toBeNull();
  });
});
