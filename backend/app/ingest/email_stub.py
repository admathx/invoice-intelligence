"""SPEC.md §10 Phase 6: email intake — the real-world entry point, minus real
mail infrastructure.

A watch directory stands in for an inbound mail server: drop an `.eml` in
`inbox/`, and this parses it, routes it to a tenant by the address it was
sent to, and turns each PDF attachment into an invoice on the same queue the
HTTP upload endpoint uses. Nothing here talks to a mail provider — swapping
in a real inbound webhook later means replacing `scan_inbox`, not the
routing/attachment logic underneath it.

SPEC.md's exit criterion is that nothing is ever *silently* dropped: an email
that can't be routed, carries no invoice, or fails to parse at all gets moved
to `inbox/quarantine/` with a `.reason.txt` beside it, so a human can see
exactly what arrived and why it didn't become an invoice.
"""
import re
import time
import uuid
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import bind_tenant
from app.ingest.upload import save_invoice_bytes
from app.models import Invoice, Tenant
from app.models.enums import InvoiceSource, InvoiceStatus
from app.queue import invoice_queue
from app.workers.tasks import process_invoice

# Headers a forwarded message can carry the *original* recipient in. `To`
# alone isn't enough: a restaurant forwarding a distributor's invoice usually
# leaves its own address in Delivered-To / X-Original-To while `To` becomes
# whoever they forwarded it to.
RECIPIENT_HEADERS = ("to", "cc", "bcc", "delivered-to", "x-original-to", "x-forwarded-to")

PROCESSED_DIRNAME = "processed"
QUARANTINE_DIRNAME = "quarantine"

# How long a file must sit untouched before it's considered fully written.
# See scan_inbox. Short enough that `make watch-inbox ONCE=1` still picks up
# something a human just dropped, long enough to cover a local file copy.
INBOX_SETTLE_SECONDS = 2.0


@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    content: bytes

    @property
    def is_pdf(self) -> bool:
        return self.content_type == "application/pdf" or self.filename.lower().endswith(".pdf")


@dataclass
class ParsedEmail:
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    message_id: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)

    @property
    def pdf_attachments(self) -> list[EmailAttachment]:
        return [a for a in self.attachments if a.is_pdf]


@dataclass
class IngestResult:
    source_name: str
    status: str  # "ingested" | "duplicate" | "quarantined"
    reason: str | None = None
    tenant_id: uuid.UUID | None = None
    invoice_ids: list[uuid.UUID] = field(default_factory=list)
    destination: Path | None = None


def inbox_address_for(name: str, domain: str | None = None) -> str:
    """Derives the address a tenant forwards invoices to, from its name."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug}@{domain or settings.inbox_domain}"


def parse_email(raw: bytes) -> ParsedEmail:
    message = message_from_bytes(raw, policy=policy.default)
    if not isinstance(message, EmailMessage):  # pragma: no cover - policy.default always yields EmailMessage
        raise ValueError("unsupported message type")

    header_pairs = []
    for header in RECIPIENT_HEADERS:
        header_pairs.extend((header, value) for value in message.get_all(header, []))
    recipients = [addr.lower() for _, addr in getaddresses([str(v) for _, v in header_pairs]) if addr]

    attachments: list[EmailAttachment] = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachments.append(
            EmailAttachment(
                filename=filename or "attachment",
                content_type=(part.get_content_type() or "").lower(),
                content=payload,
            )
        )

    return ParsedEmail(
        recipients=recipients,
        subject=message.get("subject"),
        message_id=message.get("message-id"),
        attachments=attachments,
    )


def find_tenant_for_recipients(db: Session, recipients: list[str]) -> Tenant | None:
    """First recipient address that maps to a tenant wins.

    Matched case-insensitively: addresses are case-insensitive in practice,
    and a tenant whose stored address differs only in case from what the mail
    server delivered would otherwise quarantine for no reason.
    """
    for address in recipients:
        tenant = db.scalar(select(Tenant).where(func.lower(Tenant.inbox_address) == address))
        if tenant is not None:
            return tenant
    return None


def _unique_destination(directory: Path, name: str) -> Path:
    """Never clobber an earlier file: the same invoice forwarded twice would
    otherwise overwrite the first copy in processed/ and lose the evidence.
    """
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    return directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def _quarantine(path: Path, inbox_dir: Path, reason: str, source_name: str | None = None) -> IngestResult:
    """Move an email aside with a readable explanation beside it.

    `source_name` overrides the reported name for a file that has already been
    moved once (claimed into processed/, then quarantined when the database
    write failed) — the caller still wants to hear about the name that arrived.
    """
    destination = _unique_destination(inbox_dir / QUARANTINE_DIRNAME, path.name)
    path.rename(destination)
    destination.with_suffix(destination.suffix + ".reason.txt").write_text(reason + "\n")
    return IngestResult(
        source_name=source_name or path.name, status="quarantined", reason=reason, destination=destination
    )


def _safe_quarantine(path: Path, inbox_dir: Path, reason: str) -> IngestResult:
    """_quarantine that reports rather than raises.

    Used only from scan_inbox's per-email failure handler, which must never
    itself throw: the exception it is handling may have come from a
    _quarantine call that already renamed the file away, in which case a second
    rename raises FileNotFoundError and takes down the whole scan — stopping
    every email queued behind this one, which is the exact outcome that handler
    exists to prevent.
    """
    if not path.exists():
        return IngestResult(
            source_name=path.name,
            status="quarantined",
            reason=f"{reason} (already moved out of the inbox before it could be quarantined)",
        )
    try:
        return _quarantine(path, inbox_dir, reason)
    except OSError as exc:
        return IngestResult(
            source_name=path.name, status="quarantined", reason=f"{reason} (could not be moved aside: {exc})"
        )


def _already_ingested(db: Session, tenant_id: uuid.UUID, message_id: str) -> bool:
    bind_tenant(db, tenant_id)
    return (
        db.scalar(
            select(Invoice.id).where(Invoice.tenant_id == tenant_id, Invoice.source_message_id == message_id).limit(1)
        )
        is not None
    )


def ingest_email_file(db: Session, path: Path, inbox_dir: Path | None = None) -> IngestResult:
    """Turns one `.eml` into invoices, or quarantines it with a reason."""
    inbox_dir = inbox_dir or Path(settings.inbox_dir)
    source_name = path.name

    try:
        parsed = parse_email(path.read_bytes())
    except Exception as exc:  # malformed mail must not take down the whole scan
        return _quarantine(path, inbox_dir, f"could not parse email: {exc}")

    # Carried into every rejection below: a human reading quarantine/ needs to
    # recognise which message this was without opening the .eml.
    context = f' (subject: "{parsed.subject}")' if parsed.subject else ""

    if not parsed.recipients:
        return _quarantine(path, inbox_dir, f"no recipient address found in headers{context}")

    tenant = find_tenant_for_recipients(db, parsed.recipients)
    if tenant is None:
        return _quarantine(
            path, inbox_dir, f"no tenant for recipient address(es): {', '.join(parsed.recipients)}{context}"
        )

    pdfs = parsed.pdf_attachments
    if not pdfs:
        other = ", ".join(a.filename for a in parsed.attachments) or "none"
        return _quarantine(path, inbox_dir, f"no PDF attachment (attachments: {other}){context}")

    if parsed.message_id and _already_ingested(db, tenant.id, parsed.message_id):
        # Filed as processed, not quarantined: nothing is wrong with this
        # email, it simply already became invoices. Quarantining it would
        # invite the operator to re-drop it and create the duplicates all over.
        destination = _unique_destination(inbox_dir / PROCESSED_DIRNAME, path.name)
        path.rename(destination)
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=f"already ingested (message-id {parsed.message_id}){context}",
            tenant_id=tenant.id,
            destination=destination,
        )

    # Claim the email BEFORE writing anything: moving it out of the inbox is
    # the only thing that stops the next scan from ingesting it again, so it
    # has to happen before the step a rerun would duplicate. Committing first
    # and moving afterwards meant a failed move (or a failed enqueue, which
    # threw into scan_inbox's handler) left committed invoices behind an email
    # reported and filed as "ingest failed" — and re-dropping it, the
    # documented recovery, produced a second full set of invoices.
    try:
        claimed = _unique_destination(inbox_dir / PROCESSED_DIRNAME, path.name)
        path.rename(claimed)
    except OSError as exc:
        return _quarantine(path, inbox_dir, f"could not claim email for processing: {exc}{context}")

    bind_tenant(db, tenant.id)
    invoice_ids: list[uuid.UUID] = []
    try:
        for attachment in pdfs:
            # One invoice per PDF: a distributor mailing a week's invoices as
            # several attachments is a single email but several invoices.
            invoice = Invoice(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source=InvoiceSource.email,
                source_message_id=parsed.message_id,
                status=InvoiceStatus.received,
                original_file_uri="",
            )
            invoice.original_file_uri = save_invoice_bytes(invoice.id, attachment.filename, attachment.content)
            db.add(invoice)
            invoice_ids.append(invoice.id)
        db.commit()
    except Exception as exc:
        db.rollback()
        return _quarantine(claimed, inbox_dir, f"could not record invoices: {exc}{context}", source_name=source_name)

    # Enqueue last, and deliberately non-fatal. The invoices are committed and
    # durable at this point; a Redis outage should leave them sitting in
    # `received` for a requeue, not re-open the question of whether this email
    # was ingested (it was) or move it somewhere a human might re-drop it.
    enqueue_error: str | None = None
    for invoice_id in invoice_ids:
        try:
            invoice_queue.enqueue(process_invoice, str(invoice_id))
        except Exception as exc:
            enqueue_error = f"invoices recorded but could not be queued for extraction: {exc}"
            break

    return IngestResult(
        source_name=source_name,
        status="ingested",
        reason=enqueue_error,
        tenant_id=tenant.id,
        invoice_ids=invoice_ids,
        destination=claimed,
    )


def scan_inbox(db: Session, inbox_dir: Path | None = None, settle_seconds: float | None = None) -> list[IngestResult]:
    """Processes every settled `.eml` in the watch directory, oldest first.

    `settle_seconds`: ignore files written within this many seconds, because
    a watch directory has no way to tell "finished" from "still arriving". A
    multi-megabyte email caught mid-write parses without raising — MIME
    parsing is tolerant — yields no complete attachments, and gets moved to
    quarantine as "no PDF attachment" while the writer is still appending to
    it, so the finished message never gets processed at all. Skipping a
    too-fresh file drops nothing: it stays in the inbox for the next pass.
    Tests pass 0 to make the scan deterministic.
    """
    inbox_dir = inbox_dir or Path(settings.inbox_dir)
    settle_seconds = INBOX_SETTLE_SECONDS if settle_seconds is None else settle_seconds
    inbox_dir.mkdir(parents=True, exist_ok=True)

    now = time.time()
    settled: list[tuple[float, Path]] = []
    for path in inbox_dir.glob("*.eml"):
        try:
            mtime = path.stat().st_mtime
        except OSError:  # vanished between glob and stat
            continue
        if now - mtime >= settle_seconds:
            settled.append((mtime, path))

    results = []
    for _, path in sorted(settled):
        try:
            results.append(ingest_email_file(db, path, inbox_dir))
        except Exception as exc:
            # One bad email must not stop the ones behind it in the directory.
            db.rollback()
            results.append(_safe_quarantine(path, inbox_dir, f"ingest failed: {exc}"))
    return results
