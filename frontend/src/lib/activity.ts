/** Turning audit events (backend app/audit.py) into sentences. */

export type AuditEvent = {
  id: string;
  occurred_at: string;
  actor_name: string | null;
  actor_email: string | null;
  action: string;
  entity_type: string;
  entity_id: string | null;
  tenant_id?: string | null;
  tenant_name?: string | null;
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
  if (event.actor_name || event.actor_email) return (event.actor_name || event.actor_email)!;
  // No actor on a failed sign-in means nobody got in, not that the system acted.
  return event.action === "auth.login_failed" ? "Someone" : "System";
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
    case "email.rejected":
      return `couldn't take an email: ${show(d.reason)}`;
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
    case "auth.login":
      return "signed in";
    case "auth.logout":
      return "signed out";
    case "auth.login_failed":
      return `failed to sign in as ${show(d.email)}`;
    case "user.created":
      return `created a login for ${show(d.email)}${d.is_operator ? " (operator)" : ""}`;
    case "user.access_granted":
      return `gave ${show(d.email)} access to ${show(d.location)}`;
    case "user.access_revoked":
      return `removed ${show(d.email)}'s access to ${show(d.location)}`;
    case "user.updated":
      return describeUserUpdate(d);
    case "user.password_changed":
      return "changed their password";
    case "auth.password_change_failed":
      return "entered the wrong current password while changing it";
    case "user.password_reset":
      return `reset ${show(d.email)}'s password`;
    case "account.created":
      return `created the business ${show(d.name)}`;
    case "tenant.created":
      return `added this location (${show(d.metro)}); invoices forward to ${show(d.inbox_address)}`;
    case "account.location_attached":
      return `added this location to ${show(d.account_name)}`;
    case "account.location_detached":
      return `removed this location from ${show(d.account_name)}`;
    default:
      return event.action.replaceAll("_", " ").replaceAll(".", ": ");
  }
}

function describeUserUpdate(d: Record<string, unknown>): string {
  const changes = (d.changes ?? {}) as Record<string, Change>;
  const who = show(d.email);
  const parts: string[] = [];
  if (changes.is_active) parts.push(`${changes.is_active.to ? "reactivated" : "deactivated"} ${who}`);
  if (changes.is_operator)
    parts.push(changes.is_operator.to ? `made ${who} an operator` : `removed ${who}'s operator access`);
  if (changes.name) parts.push(`renamed ${who} from ${show(changes.name.from)} to ${show(changes.name.to)}`);
  return parts.length ? parts.join("; ") : `changed ${who}`;
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


/** What kind of thing happened, for the color it's shown in: problems red,
 *  invoices blue, matching and pricing green, access amber, sign-ins gray. */
export type EventKind = "problem" | "invoice" | "review" | "access" | "auth";

export function eventKind(event: AuditEvent): EventKind {
  const a = event.action;
  if (a === "email.rejected" || a.endsWith("_failed") || a === "invoice.extraction_failed") return "problem";
  if (a.startsWith("invoice_line.")) return "review";
  if (a.startsWith("invoice.")) return "invoice";
  if (a.startsWith("auth.")) return "auth";
  return "access";
}

export const EVENT_DOT: Record<EventKind, string> = {
  problem: "bg-red-500",
  invoice: "bg-sky-500",
  review: "bg-brand-500",
  access: "bg-amber-400",
  auth: "bg-gray-300",
};
