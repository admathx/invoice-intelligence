import { describe, expect, it } from "vitest";

import { formatApiError } from "./apiError";

describe("formatApiError", () => {
  it("passes a plain string detail through, so upload rejections say why", () => {
    expect(formatApiError("file is not a PDF", "Upload failed.")).toBe("file is not a PDF");
  });

  it("joins the review endpoints' reasons", () => {
    expect(
      formatApiError({ message: "invoice still doesn't reconcile", reasons: ["a", "b"] }, "fallback")
    ).toBe("invoice still doesn't reconcile: a; b");
  });

  it("names the field in a validation error instead of a generic failure", () => {
    const detail = [{ loc: ["body", "line_items", 0, "unit_price"], msg: "Input should be a valid decimal" }];
    expect(formatApiError(detail, "fallback")).toBe(
      "unit price: enter a plain number, like 1234.50 (no $ or commas)"
    );
  });

  it("lets a screen map array positions back to what it showed", () => {
    const detail = [{ loc: ["body", "line_items", 1, "quantity"], msg: "Input should be a valid decimal" }];
    const label = (loc: (string | number)[]) => (loc[1] === "line_items" ? `line 7 ${String(loc[3])}` : null);
    expect(formatApiError(detail, "fallback", label)).toMatch(/^line 7 quantity: /);
  });

  it("falls back only when there is genuinely nothing to show", () => {
    expect(formatApiError(undefined, "Request failed.")).toBe("Request failed.");
    expect(formatApiError([], "Request failed.")).toBe("Request failed.");
  });
});
