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
  quantity: "qty",
  unit_price: "price each",
  extended_price: "line total",
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
  // No actor on a failed sign-in (or a reset request) means nobody got in,
  // not that the system acted.
  return event.action === "auth.login_failed" || event.action === "auth.password_reset_requested" ? "Someone" : "System";
}

const DOCUMENT_LABEL: Record<string, string> = { statement: "a statement", price_list: "a price list" };

const LINE = (d: Record<string, unknown>) => (d.line_number ? `item ${d.line_number}` : "an item");

/** One-line summary. Details beyond it (per-field changes) are rendered by
 *  the caller from describeChanges. */
export function describeEvent(event: AuditEvent): string {
  const d = event.details;
  switch (event.action) {
    case "invoice.uploaded":
      return typeof d.photo_count === "number"
        ? `uploaded ${d.photo_count} photo${d.photo_count === 1 ? "" : "s"} of a paper invoice`
        : `uploaded ${show(d.filename)}`;
    case "invoice.exported":
      return `exported ${d.kind === "line-items" ? "line items" : "invoices"} to a spreadsheet (${exportPeriod(d)}, ${show(d.rows)} rows)`;
    case "invoice.received_by_email":
      return d.photos ? `received photos of an invoice by email (${show(d.photos)})` : `received ${show(d.filename)} by email`;
    case "invoice.extracted":
      if (d.duplicate_of) return `read ${show(d.line_count)} items; it looks like a copy of an invoice already added`;
      if (d.document_type)
        return `read it; it looks like ${DOCUMENT_LABEL[String(d.document_type)] ?? "something other than an invoice"}, not an invoice`;
      if (d.billed_to) return `read ${show(d.line_count)} items; it's made out to ${show(d.billed_to)}, not this restaurant`;
      if (typeof d.other_invoices_in_file === "number")
        return `read ${show(d.line_count)} items; the file held ${d.other_invoices_in_file} more invoice${d.other_invoices_in_file === 1 ? "" : "s"}, added on ${d.other_invoices_in_file === 1 ? "its" : "their"} own`;
      if (d.another_invoice_on_its_page)
        return `read ${show(d.line_count)} items; its page also shows another invoice, which wasn't read`;
      if (d.counted_as_credits)
        return `read ${show(d.line_count)} items; a credit memo printed with positive amounts, so they count as credits`;
      return d.status === "extracted"
        ? `read ${show(d.line_count)} items; everything added up`
        : `read ${show(d.line_count)} items; some numbers need a look`;
    case "invoice.deleted":
      return `deleted ${d.invoice_number ? `invoice ${show(d.invoice_number)}` : "an invoice"}${d.distributor ? ` from ${show(d.distributor)}` : ""}${d.total ? ` (total ${show(d.total)})` : ""}`;
    case "invoice.kept":
      return d.copy_of ? "said the invoice isn't a copy" : d.billed_to ? "said the invoice is this restaurant's" : "said it is an invoice";
    case "invoice.split_out":
      return "found this invoice in a file with others, and added it on its own";
    case "invoice.duplicate_file_skipped":
      return `skipped ${show(d.filename)} from an email: that file was already added`;
    case "distributor.added":
      return `added the distributor ${show(d.name)}`;
    case "invoice_line.pack_set":
      return `set the pack size of ${show(d.raw_description)}${
        d.applied_to ? `, filled in on ${show(d.applied_to)} other invoice${d.applied_to === 1 ? "" : "s"}` : ""
      }${d.remembered ? " (remembered for this item)" : ""}`;
    case "invoice_line.not_a_product":
      return `marked ${show(d.raw_description)} as a fee or charge, not a product`;
    case "email.rejected":
      return `couldn't use an emailed invoice: ${show(d.reason)}`;
    case "invoice.extraction_failed":
      return "couldn't read the invoice";
    case "invoice.edited":
      return "fixed the invoice";
    case "invoice.confirmed":
      return `confirmed the invoice (total ${show(d.total)})`;
    case "invoice_line.added":
      return `added ${LINE(d)}`;
    case "invoice_line.removed":
      return `removed ${LINE(d)} (${show((d.values as Record<string, unknown> | undefined)?.raw_description)})`;
    case "invoice_line.match_confirmed":
      return `confirmed which product ${show(d.raw_description)} is`;
    case "invoice_line.match_corrected":
      return `changed which product ${show(d.raw_description)} is`;
    case "price_alert.dismissed":
      return `took ${show(d.product)} off price alerts`;
    case "invoice.requeued":
      return "tried reading the invoice again";
    case "invoice_line.reopened":
      return `sent ${show(d.raw_description)} back to be matched again`;
    case "auth.login":
      return "signed in";
    case "auth.logout":
      return "signed out";
    case "auth.login_failed":
      return `failed to sign in as ${show(d.email)}`;
    case "user.created":
      return `created a login for ${show(d.email)}${d.is_operator ? " (admin)" : ""}`;
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
    case "auth.password_reset_requested":
      return d.sent ? `asked for a password reset link for ${show(d.email)}` : `asked for a password reset link for ${show(d.email)} (none sent)`;
    case "user.password_reset_by_email":
      return "chose a new password from an emailed link";
    case "user.digest_subscribed":
      return "turned the weekly summary on";
    case "user.digest_unsubscribed":
      return "turned the weekly summary off";
    case "user.alert_emails_subscribed":
      return "turned price-increase emails on";
    case "user.alert_emails_unsubscribed":
      return "turned price-increase emails off";
    case "account.created":
      return `created the business ${show(d.name)}`;
    case "tenant.created":
      return `added this location; its invoice email is ${show(d.inbox_address)}`;
    case "account.location_attached":
      return `added this location to ${show(d.account_name)}`;
    case "account.location_detached":
      return `removed this location from ${show(d.account_name)}`;
    default:
      return event.action.replaceAll("_", " ").replaceAll(".", ": ");
  }
}

function exportPeriod(d: Record<string, unknown>): string {
  if (!d.start && !d.end) return "all dates";
  if (d.start && d.end) return `${d.start} to ${d.end}`;
  return d.start ? `from ${d.start}` : `up to ${d.end}`;
}

function describeUserUpdate(d: Record<string, unknown>): string {
  const changes = (d.changes ?? {}) as Record<string, Change>;
  const who = show(d.email);
  const parts: string[] = [];
  if (changes.is_active) parts.push(`${changes.is_active.to ? "reactivated" : "deactivated"} ${who}`);
  if (changes.is_operator)
    parts.push(changes.is_operator.to ? `made ${who} an admin` : `removed ${who}'s admin access`);
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
    for (const text of describeChanges(changes)) lines.push(`item ${lineNumber}: ${text}`);
  }
  return lines;
}

/** Where an event is about an invoice, the invoice to link to. */
export function eventInvoiceId(event: AuditEvent): string | null {
  // Nothing to open once it's gone.
  if (event.action === "invoice.deleted") return null;
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
