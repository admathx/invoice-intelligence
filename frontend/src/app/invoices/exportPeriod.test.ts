import { describe, expect, it } from "vitest";

import { exportUrl, periodRange } from "./exportPeriod";

// Local time, like the page: 15 Feb 2026.
const today = new Date(2026, 1, 15);

describe("periodRange", () => {
  it("covers whole calendar months and quarters, inclusive", () => {
    expect(periodRange("this-month", today)).toEqual({ start: "2026-02-01", end: "2026-02-28" });
    expect(periodRange("last-month", today)).toEqual({ start: "2026-01-01", end: "2026-01-31" });
    expect(periodRange("this-quarter", today)).toEqual({ start: "2026-01-01", end: "2026-03-31" });
    expect(periodRange("last-quarter", today)).toEqual({ start: "2025-10-01", end: "2025-12-31" });
  });

  it("crosses the year boundary", () => {
    const january = new Date(2026, 0, 3);
    expect(periodRange("last-month", january)).toEqual({ start: "2025-12-01", end: "2025-12-31" });
    expect(periodRange("last-year", january)).toEqual({ start: "2025-01-01", end: "2025-12-31" });
    expect(periodRange("year-to-date", january)).toEqual({ start: "2026-01-01", end: "2026-01-03" });
  });

  it("leaves all time open at both ends", () => {
    expect(periodRange("all", today)).toEqual({ start: null, end: null });
  });
});

describe("exportUrl", () => {
  it("sends only the filters that are set", () => {
    expect(exportUrl("invoices", "loc", { start: null, end: null }, "")).toBe("/api/exports/invoices?tenant_id=loc");
    expect(exportUrl("line-items", "loc", { start: "2026-01-01", end: "2026-01-31" }, "d1")).toBe(
      "/api/exports/line-items?tenant_id=loc&start=2026-01-01&end=2026-01-31&distributor_id=d1",
    );
  });
});
