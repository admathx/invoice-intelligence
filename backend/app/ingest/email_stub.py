"""SPEC.md §10 Phase 6: email intake.

An email is parsed, routed to a tenant by the address it was sent to, and
each PDF attachment becomes an invoice on the same queue the HTTP upload
endpoint uses. An email with no PDF but photos of a paper invoice (someone
at the back door emailing what their phone took) becomes one invoice, a page
per photo; logos and signature images are left out (see is_invoice_photo). Two ways in, one path underneath (route_email ->
record_invoices -> enqueue_invoices):

- the inbound webhook (app/api/inbound.py), which a mail provider posts each
  message to: production;
- a watch directory (`inbox/`, scan_inbox): drop an `.eml` in, for local
  development and demos without a mail provider.

SPEC.md's exit criterion is that nothing is ever *silently* dropped: an email
that can't be routed, carries no invoice, or fails to parse at all gets moved
to `inbox/quarantine/` with a `.reason.txt` beside it, so a human can see
exactly what arrived and why it didn't become an invoice.
"""
import hashlib
import io
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

from app import audit
from app.config import settings
from app.db import bind_tenant
from app.ingest.photos import MAX_PHOTOS, image_kind
from app.ingest.upload import (
    InvalidInvoiceFileError,
    invoice_pdf_from_upload,
    is_pdf_bytes,
    save_invoice_bytes,
    validate_invoice_bytes,
)
from app.storage import forget_original
from app.models import Invoice, Tenant
from app.models.enums import InvoiceSource, InvoiceStatus
from app.queue import enqueue_extraction

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


# What a photo of an invoice at least is. A phone photo is hundreds of KB
# and thousands of pixels across; a signature logo or a social-media icon is
# a few KB and a couple of hundred pixels.
MIN_PHOTO_BYTES = 30 * 1024
MIN_PHOTO_SHORT_SIDE = 600


@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    content: bytes
    # "inline" images with a Content-ID are pictures inside the message body
    # (a logo in a signature), not something the sender attached.
    disposition: str | None = None
    content_id: str | None = None

    @property
    def is_invoice_photo(self) -> bool:
        """A photo someone attached, big enough to be a page of an invoice."""
        if image_kind(self.content) is None:
            return False
        if self.content_id and self.disposition != "attachment":
            return False
        if len(self.content) < MIN_PHOTO_BYTES:
            return False
        try:
            from PIL import Image

            width, height = Image.open(io.BytesIO(self.content)).size
        except Exception:
            # Not something we can open: leave it for the upload path to
            # refuse with a reason, rather than guessing here.
            return True
        return min(width, height) >= MIN_PHOTO_SHORT_SIDE

    @property
    def is_pdf(self) -> bool:
        # Decided by the bytes, not by the sender's label: a signature image or
        # a .docx named "invoice.pdf" used to become an invoice row that only
        # failed later in the renderer. A mislabelled attachment now counts as
        # a non-PDF, so an email carrying only that quarantines with a reason.
        return is_pdf_bytes(self.content)


@dataclass
class ParsedEmail:
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    message_id: str | None = None
    attachments: list[EmailAttachment] = field(default_factory=list)

    @property
    def pdf_attachments(self) -> list[EmailAttachment]:
        return [a for a in self.attachments if a.is_pdf]

    @property
    def photo_attachments(self) -> list[EmailAttachment]:
        return [a for a in self.attachments if a.is_invoice_photo]


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
                disposition=part.get_content_disposition(),
                content_id=part.get("content-id"),
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


@dataclass
class _Routed:
    """An email that can become invoices: whose they are, and which PDFs, or
    the one PDF its photos became."""

    parsed: ParsedEmail
    tenant: Tenant
    pdfs: list[EmailAttachment]
    # What makes a second delivery of this message recognisable: its
    # Message-ID, or when it has none (scanners, some relays), a hash of the
    # message itself, which a provider's retry reproduces byte for byte.
    # Stored as the invoices' source_message_id.
    dedupe_key: str
    # Set when the email's photos became its one invoice: their filenames.
    photo_names: str | None = None

    @property
    def context(self) -> str:
        return _context(self.parsed)


@dataclass
class _Rejected:
    reason: str
    parsed: ParsedEmail | None = None
    tenant: Tenant | None = None


def _context(parsed: ParsedEmail | None) -> str:
    # Carried into every rejection: a human reading it needs to recognise
    # which message this was without opening the raw email.
    return f' (subject: "{parsed.subject}")' if parsed and parsed.subject else ""


def route_email(db: Session, raw: bytes) -> _Routed | _Rejected:
    """Everything that decides whether an email becomes invoices, and whose,
    without writing anything. Shared by the watch directory and the inbound
    webhook (app/api/inbound.py), so the two can't disagree."""
    try:
        parsed = parse_email(raw)
    except Exception as exc:  # malformed mail must not take down the caller
        return _Rejected(f"could not parse email: {exc}")

    context = _context(parsed)
    if not parsed.recipients:
        return _Rejected(f"no recipient address found in headers{context}", parsed)

    tenant = find_tenant_for_recipients(db, parsed.recipients)
    if tenant is None:
        return _Rejected(f"no tenant for recipient address(es): {', '.join(parsed.recipients)}{context}", parsed)

    pdfs = parsed.pdf_attachments
    if not pdfs:
        # PDFs win when there are both: an invoice PDF with a photo or a
        # logo alongside is the PDF.
        photos = parsed.photo_attachments
        if photos:
            return _route_photos(parsed, tenant, photos, raw)
        other = ", ".join(a.filename for a in parsed.attachments) or "none"
        return _Rejected(f"no PDF or photo of an invoice attached (attachments: {other}){context}", parsed, tenant)

    # The same gate the upload endpoint applies, so the two entry points can't
    # disagree about what's acceptable. Email used to skip the size limit and
    # write a 300 MB scan to disk, invoice it, and hand it to the renderer.
    # Rejected as a whole, not dropped attachment by attachment: nothing in
    # an email is silently discarded (SPEC.md §10 Phase 6).
    for attachment in pdfs:
        try:
            validate_invoice_bytes(attachment.content)
        except InvalidInvoiceFileError as exc:
            return _Rejected(f"attachment {attachment.filename!r} rejected: {exc}{context}", parsed, tenant)

    return _Routed(parsed, tenant, pdfs, _dedupe_key(parsed, raw))


def _dedupe_key(parsed: ParsedEmail, raw: bytes) -> str:
    return parsed.message_id or f"<sha256:{hashlib.sha256(raw).hexdigest()}@no-message-id>"


def _route_photos(parsed: ParsedEmail, tenant: Tenant, photos: list[EmailAttachment], raw: bytes) -> _Routed | _Rejected:
    """Every photo in the email is a page of one invoice, in the order
    attached: that's how a phone sends the pages of one paper invoice."""
    context = _context(parsed)
    if len(photos) > MAX_PHOTOS:
        return _Rejected(f"{len(photos)} photos attached; one invoice can have up to {MAX_PHOTOS}{context}", parsed, tenant)
    try:
        pdf, _ = invoice_pdf_from_upload([a.content for a in photos])
    except InvalidInvoiceFileError as exc:
        return _Rejected(f"photos rejected: {exc}{context}", parsed, tenant)
    as_pdf = EmailAttachment(filename=photos[0].filename, content_type="application/pdf", content=pdf)
    return _Routed(
        parsed, tenant, [as_pdf], _dedupe_key(parsed, raw), photo_names=", ".join(a.filename for a in photos)
    )


def _lock_message(db: Session, tenant_id: uuid.UUID, message_id: str) -> None:
    """Serialize concurrent deliveries of the same message (a provider
    retrying while the first attempt is still running) until this
    transaction ends, so the duplicate check below can't pass for both."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"{tenant_id}|{message_id}", 0))))


def record_invoices(db: Session, routed: _Routed) -> list[uuid.UUID] | None:
    """One invoice per PDF, committed. None if this message already became
    invoices (checked again under a lock, see _lock_message)."""
    tenant = routed.tenant
    bind_tenant(db, tenant.id)
    _lock_message(db, tenant.id, routed.dedupe_key)
    if _already_ingested(db, tenant.id, routed.dedupe_key):
        db.rollback()
        return None
    invoice_ids: list[uuid.UUID] = []
    try:
        _add_invoices(db, routed, invoice_ids)
        db.commit()
    except Exception:
        # Files are stored as each invoice is built, before the commit; if the
        # commit doesn't happen, they'd be left referenced by nothing (and a
        # provider's retry would store another copy each time).
        db.rollback()
        for invoice_id in invoice_ids:
            forget_original(invoice_id)
        raise
    return invoice_ids


def _add_invoices(db: Session, routed: _Routed, invoice_ids: list[uuid.UUID]) -> None:
    tenant, parsed = routed.tenant, routed.parsed
    for attachment in routed.pdfs:
        # One invoice per PDF: a distributor mailing a week's invoices as
        # several attachments is a single email but several invoices.
        invoice = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            source=InvoiceSource.email,
            source_message_id=routed.dedupe_key,
            status=InvoiceStatus.received,
            original_file_uri="",
        )
        invoice_ids.append(invoice.id)  # before the write, so a failed write is cleaned up too
        invoice.original_file_uri = save_invoice_bytes(invoice.id, attachment.filename, attachment.content)
        db.add(invoice)
        audit.record(
            db,
            None,
            "invoice.received_by_email",
            "invoice",
            invoice.id,
            tenant.id,
            filename=attachment.filename,
            photos=routed.photo_names,
            message_id=parsed.message_id,
        )


def enqueue_invoices(invoice_ids: list[uuid.UUID]) -> str | None:
    """Queue each for extraction. Deliberately non-fatal: the invoices are
    committed and durable; a Redis outage should leave them in `received` for
    a requeue, not reopen whether the email was ingested (it was)."""
    for invoice_id in invoice_ids:
        try:
            enqueue_extraction(invoice_id)
        except Exception as exc:
            return f"invoices recorded but could not be queued for extraction: {exc}"
    return None


def ingest_email_bytes(db: Session, raw: bytes, source_name: str) -> IngestResult:
    """The inbound webhook's path: one email's bytes in, invoices or a reason
    out. A rejection is returned, not stored; the caller decides where a
    rejected email goes. Database errors propagate, so the provider retries
    (safe: a retry of an ingested message comes back as a duplicate)."""
    routed = route_email(db, raw)
    if isinstance(routed, _Rejected):
        return IngestResult(
            source_name=source_name,
            status="quarantined",
            reason=routed.reason,
            tenant_id=routed.tenant.id if routed.tenant else None,
        )
    invoice_ids = record_invoices(db, routed)
    if invoice_ids is None:
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=f"already ingested (message-id {routed.dedupe_key}){routed.context}",
            tenant_id=routed.tenant.id,
        )
    return IngestResult(
        source_name=source_name,
        status="ingested",
        reason=enqueue_invoices(invoice_ids),
        tenant_id=routed.tenant.id,
        invoice_ids=invoice_ids,
    )


def ingest_email_file(db: Session, path: Path, inbox_dir: Path | None = None) -> IngestResult:
    """Turns one `.eml` from the watch directory into invoices, or
    quarantines it with a reason."""
    inbox_dir = inbox_dir or Path(settings.inbox_dir)
    source_name = path.name

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _quarantine(path, inbox_dir, f"could not read email: {exc}")
    routed = route_email(db, raw)
    if isinstance(routed, _Rejected):
        return _quarantine(path, inbox_dir, routed.reason)
    tenant, context = routed.tenant, routed.context

    if _already_ingested(db, tenant.id, routed.dedupe_key):
        # Filed as processed, not quarantined: nothing is wrong with this
        # email, it simply already became invoices. Quarantining it would
        # invite the operator to re-drop it and create the duplicates all over.
        destination = _unique_destination(inbox_dir / PROCESSED_DIRNAME, path.name)
        path.rename(destination)
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=f"already ingested (message-id {routed.dedupe_key}){context}",
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

    try:
        invoice_ids = record_invoices(db, routed)
    except Exception as exc:
        db.rollback()
        return _quarantine(claimed, inbox_dir, f"could not record invoices: {exc}{context}", source_name=source_name)
    if invoice_ids is None:  # another delivery of the same message won the race
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=f"already ingested (message-id {routed.dedupe_key}){context}",
            tenant_id=tenant.id,
            destination=claimed,
        )

    return IngestResult(
        source_name=source_name,
        status="ingested",
        reason=enqueue_invoices(invoice_ids),
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
