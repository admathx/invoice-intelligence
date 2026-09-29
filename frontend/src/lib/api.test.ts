import { describe, expect, it } from "vitest";

import { isPublicPath, safeNext } from "./api";

describe("isPublicPath", () => {
  it("is the signed-out pages and nothing that merely starts like them", () => {
    for (const path of ["/login", "/forgot-password", "/reset-password", "/help"]) expect(isPublicPath(path)).toBe(true);
    expect(isPublicPath("/invoices")).toBe(false);
    expect(isPublicPath("/login-as-admin")).toBe(false);
  });
});

describe("safeNext", () => {
  it("only follows paths on this site", () => {
    expect(safeNext("/negotiation")).toBe("/negotiation");
    expect(safeNext("//evil.example")).toBe("/invoices");
    expect(safeNext("https://evil.example")).toBe("/invoices");
  });
});
