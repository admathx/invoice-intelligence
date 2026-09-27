import { describe, expect, it } from "vitest";

import { actorLabel, describeEvent, eventDetailLines, eventInvoiceId, type AuditEvent } from "./activity";

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

  it("says why an email didn't become invoices", () => {
    expect(
      describeEvent(event({ action: "email.rejected", details: { reason: "no PDF attachment (attachments: none)" } })),
    ).toBe("couldn't take an email: no PDF attachment (attachments: none)");
  });

  it("falls back to something readable for an action it doesn't know", () => {
    expect(describeEvent(event({ action: "vendor.renamed_thing" }))).toBe("vendor: renamed thing");
  });
});
