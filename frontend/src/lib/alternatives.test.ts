import { describe, expect, it } from "vitest";

import { alternativeSource, type Alternative } from "./alternatives";

const base: Alternative = {
  distributor_name: "US Foods",
  price: "8.500000",
  saving_pct: "0.1500",
  yours: true,
  last_bought: "2026-09-12",
  distinct_account_count: null,
  scope: null,
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
