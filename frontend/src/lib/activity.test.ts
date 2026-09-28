import { describe, expect, it } from "vitest";

import { actorLabel, describeEvent, eventDetailLines, eventInvoiceId, eventKind, type AuditEvent } from "./activity";

function event(partial: Partial<AuditEvent>): AuditEvent {
  return {
    id: "e1",
    occurred_at: "2026-09-26T12:00:00Z",
    actor_name: null,
    actor_email: null,
    action: "invoice.edited",
    entity_type: "invoice",
    entity_id: "inv-1",
    details: {},
    ...partial,
  };
}

describe("activity", () => {
  it("lists what an edit changed, invoice fields first, then each line", () => {
    const lines = eventDetailLines(
      event({
        details: {
          changes: { total: { from: "20.00", to: "24.00" }, distributor_id: { from: null, to: "d-2" } },
          line_changes: { "3": { unit_price: { from: "74.50", to: "47.50" }, raw_sku: { from: null, to: "A1" } } },
        },
      }),
    );
    expect(lines).toEqual([
      "total 20.00 → 24.00",
      "distributor changed",
      "line 3: unit price 74.50 → 47.50",
      "line 3: item code blank → A1",
    ]);
  });

  it("calls an event with no person the system", () => {
    expect(actorLabel(event({}))).toBe("System");
    expect(actorLabel(event({ actor_email: "a@b.test" }))).toBe("a@b.test");
    expect(actorLabel(event({ actor_name: "Ana", actor_email: "a@b.test" }))).toBe("Ana");
    expect(actorLabel(event({ action: "auth.login_failed" }))).toBe("Someone");
  });

  it("says whether extraction's numbers added up", () => {
    expect(describeEvent(event({ action: "invoice.extracted", details: { status: "extracted", line_count: 12 } }))).toBe(
      "read 12 lines; the numbers added up",
    );
    expect(
      describeEvent(event({ action: "invoice.extracted", details: { status: "needs_review", line_count: 3 } })),
    ).toBe("read 3 lines; held for review");
  });

  it("links a line's event to its invoice, even once the line is gone", () => {
    expect(
      eventInvoiceId(event({ action: "invoice_line.removed", entity_type: "invoice_line_item", entity_id: "l-1", details: { invoice_id: "inv-9" } })),
    ).toBe("inv-9");
    expect(eventInvoiceId(event({ entity_type: "account", entity_id: "a-1" }))).toBeNull();
  });

  it("describes user management in words", () => {
    expect(
      describeEvent(event({ action: "user.access_granted", details: { email: "a@b.test", location: "Cedar Table" } })),
    ).toBe("gave a@b.test access to Cedar Table");
    expect(
      describeEvent(event({ action: "user.updated", details: { email: "a@b.test", changes: { is_active: { from: true, to: false } } } })),
    ).toBe("deactivated a@b.test");
    expect(
      describeEvent(event({ action: "user.updated", details: { email: "a@b.test", changes: { is_operator: { from: false, to: true } } } })),
    ).toBe("made a@b.test an operator");
  });

  it("names a new location's forwarding address", () => {
    expect(
      describeEvent(event({ action: "tenant.created", details: { metro: "Austin, TX", inbox_address: "x@invoices.example.com" } })),
    ).toBe("added this location (Austin, TX); invoices forward to x@invoices.example.com");
  });

  it("tells a password change from a sign-in", () => {
    expect(describeEvent(event({ action: "user.password_changed", actor_name: "Ana" }))).toBe("changed their password");
    expect(describeEvent(event({ action: "auth.password_change_failed", actor_name: "Ana" }))).toBe(
      "entered the wrong current password while changing it",
    );
  });

  it("says why an email didn't become invoices", () => {
    expect(
      describeEvent(event({ action: "email.rejected", details: { reason: "no PDF attachment (attachments: none)" } })),
    ).toBe("couldn't take an email: no PDF attachment (attachments: none)");
  });

  it("colors events by what kind of thing happened", () => {
    expect(eventKind(event({ action: "invoice.extraction_failed" }))).toBe("problem");
    expect(eventKind(event({ action: "auth.login_failed" }))).toBe("problem");
    expect(eventKind(event({ action: "email.rejected" }))).toBe("problem");
    expect(eventKind(event({ action: "invoice_line.match_corrected" }))).toBe("review");
    expect(eventKind(event({ action: "invoice.confirmed" }))).toBe("invoice");
    expect(eventKind(event({ action: "auth.login" }))).toBe("auth");
    expect(eventKind(event({ action: "user.access_granted" }))).toBe("access");
  });

  it("falls back to something readable for an action it doesn't know", () => {
    expect(describeEvent(event({ action: "vendor.renamed_thing" }))).toBe("vendor: renamed thing");
  });
});

describe("describing the newer events", () => {
  const event = (action: string, details: Record<string, unknown>): AuditEvent => ({
    id: "1",
    occurred_at: "2026-09-28T12:00:00Z",
    actor_name: null,
    actor_email: null,
    action,
    entity_type: "invoice",
    entity_id: null,
    details,
  });

  it("says a photo upload was photos", () => {
    expect(describeEvent(event("invoice.uploaded", { filename: "IMG_1.jpg", photo_count: 2 }))).toBe(
      "uploaded 2 photos of a paper invoice",
    );
    expect(describeEvent(event("invoice.uploaded", { filename: "inv.pdf" }))).toBe("uploaded inv.pdf");
  });

  it("names what was exported and for when", () => {
    expect(describeEvent(event("invoice.exported", { kind: "line-items", start: "2026-05-01", end: "2026-05-31", rows: 40 }))).toBe(
      "exported line items to a spreadsheet (2026-05-01 to 2026-05-31, 40 rows)",
    );
    expect(describeEvent(event("invoice.exported", { kind: "invoices", rows: 3 }))).toBe(
      "exported invoices to a spreadsheet (all dates, 3 rows)",
    );
  });

  it("credits an anonymous reset request to someone, not the system", () => {
    expect(actorLabel(event("auth.password_reset_requested", { email: "a@b.c", sent: false }))).toBe("Someone");
  });
});
