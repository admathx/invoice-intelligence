import { describe, expect, it } from "vitest";

import { isPdf, isPhoto } from "./uploadFiles";

describe("isPdf / isPhoto", () => {
  it("goes by the type the browser reports", () => {
    expect(isPdf({ name: "scan", type: "application/pdf" })).toBe(true);
    expect(isPhoto({ name: "IMG_1", type: "image/jpeg" })).toBe(true);
    expect(isPhoto({ name: "invoice.pdf", type: "application/pdf" })).toBe(false);
  });

  it("falls back to the extension when there's no type (HEIC in Chrome)", () => {
    expect(isPhoto({ name: "IMG_0042.HEIC", type: "" })).toBe(true);
    expect(isPdf({ name: "invoice.PDF", type: "" })).toBe(true);
  });

  it("is neither for anything else", () => {
    const docx = { name: "invoice.docx", type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" };
    expect(isPdf(docx)).toBe(false);
    expect(isPhoto(docx)).toBe(false);
  });
});
