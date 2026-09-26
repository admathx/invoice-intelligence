/** Turning audit events (backend app/audit.py) into sentences. */

export type AuditEvent = {
  id: string;
  occurred_at: string;
  actor_name: string | null;
  actor_email: string | null;
  action: string;
  entity_type: string;
  entity_id: string | null;
  details: Record<string, unknown>;
};

type Change = { from?: unknown; to?: unknown };

const FIELD_LABELS: Record<string, string> = {
  invoice_date: "date",
  distributor_id: "distributor",
  raw_description: "description",
  raw_sku: "item code",
  raw_pack_size: "pack size",
  uom: "unit",
  unit_price: "unit price",
  extended_price: "extended price",
};

export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field.replaceAll("_", " ");
}

function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "blank";
  return String(value);
}

/** "total 20.00 → 24.00" for each changed field. Distributor ids are
 *  opaque, so they're named as changed without the ids. */
export function describeChanges(changes: Record<string, Change> | undefined): string[] {
  return Object.entries(changes ?? {}).map(([field, change]) =>
    field === "distributor_id"
      ? "distributor changed"
      : `${fieldLabel(field)} ${show(change.from)} → ${show(change.to)}`,
  );
}

export function actorLabel(event: AuditEvent): string {
  return event.actor_name || event.actor_email || "System";
}

const LINE = (d: Record<string, unknown>) => (d.line_number ? `line ${d.line_number}` : "a line");

/** One-line summary. Details beyond it (per-field changes) are rendered by
 *  the caller from describeChanges. */
export function describeEvent(event: AuditEvent): string {
  const d = event.details;
  switch (event.action) {
    case "invoice.uploaded":
      return `uploaded ${show(d.filename)}`;
    case "invoice.received_by_email":
      return `received ${show(d.filename)} by email`;
    case "invoice.extracted":
      return d.status === "extracted"
        ? `read ${show(d.line_count)} lines; the numbers added up`
        : `read ${show(d.line_count)} lines; held for review`;
    case "invoice.extraction_failed":
      return "couldn't read the invoice";
    case "invoice.edited":
      return "corrected the invoice";
    case "invoice.confirmed":
      return `confirmed the invoice (total ${show(d.total)})`;
    case "invoice_line.added":
      return `added ${LINE(d)}`;
    case "invoice_line.removed":
      return `removed ${LINE(d)} (${show((d.values as Record<string, unknown> | undefined)?.raw_description)})`;
    case "invoice_line.match_confirmed":
      return `confirmed the product match for ${show(d.raw_description)}`;
    case "invoice_line.match_corrected":
      return `corrected the product match for ${show(d.raw_description)}`;
    case "invoice_line.reopened":
      return `sent ${show(d.raw_description)} back to review`;
    case "account.location_attached":
      return `added this location to ${show(d.account_name)}`;
    case "account.location_detached":
      return `removed this location from ${show(d.account_name)}`;
    default:
      return event.action.replaceAll("_", " ").replaceAll(".", ": ");
  }
}

/** The per-field detail lines under an event, if it has any. */
export function eventDetailLines(event: AuditEvent): string[] {
  if (event.action !== "invoice.edited") return [];
  const d = event.details as {
    changes?: Record<string, Change>;
    line_changes?: Record<string, Record<string, Change>>;
  };
  const lines = describeChanges(d.changes);
  for (const [lineNumber, changes] of Object.entries(d.line_changes ?? {})) {
    for (const text of describeChanges(changes)) lines.push(`line ${lineNumber}: ${text}`);
  }
  return lines;
}

/** Where an event is about an invoice, the invoice to link to. */
export function eventInvoiceId(event: AuditEvent): string | null {
  if (event.entity_type === "invoice") return event.entity_id;
  return typeof event.details.invoice_id === "string" ? event.details.invoice_id : null;
}
