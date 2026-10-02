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
import base64
import hashlib
import io
import quopri
import re
import time
import uuid
import zipfile
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
from app.duplicates import file_hash, same_file
from app.ingest.upload import (
    InvalidInvoiceFileError,
    _opened,
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
# a few KB and a couple of hundred pixels; a banner is far wider than tall.
MIN_PHOTO_BYTES = 30 * 1024
MIN_PHOTO_SHORT_SIDE = 600
MAX_PHOTO_ASPECT = 3.0
# Taller than wide is a different matter: a till receipt photographed or
# cropped whole is easily four or five times as tall as it is wide, and the
# rule above (which is about banners) turned those away as "no photo of an
# invoice attached".
MAX_TALL_PHOTO_ASPECT = 10.0

# How far a forwarded message is followed into the messages attached to it
# (an invoice forwarded on by four people in turn arrived five deep, and was
# turned away at three), how many files are taken from a zip, and how far
# into the zips inside it.
MAX_FORWARD_DEPTH = 10
MAX_ZIP_FILES = 50
MAX_ZIP_DEPTH = 3
# Who a delivery-failure notice comes from.
_BOUNCE_SENDERS = ("mailer-daemon", "postmaster")
_ZIP_TYPES = {"application/zip", "application/x-zip-compressed", "application/x-zip"}


@dataclass
class EmailAttachment:
    filename: str
    content_type: str
    content: bytes
    disposition: str | None = None
    content_id: str | None = None
    # Why the whole email is turned away on its account: a zip that couldn't
    # be taken in full (route_email).
    problem: str | None = None

    @property
    def is_pdf(self) -> bool:
        # Decided by the bytes, not by the sender's label: a signature image or
        # a .docx named "invoice.pdf" used to become an invoice row that only
        # failed later in the renderer. A mislabelled attachment now counts as
        # a non-PDF, so an email carrying only that quarantines with a reason.
        return is_pdf_bytes(self.content)

    @property
    def is_invoice_photo(self) -> bool:
        """A picture big enough, and shaped enough like a page, to be a photo
        of an invoice.

        Deliberately not "attached rather than inline": Apple Mail (iPhone
        and Mac) sends photos inline with a Content-ID so they show in the
        message, and that's how most of these arrive. Logos and banners are
        caught by size and shape instead. (Pictures a newsletter's HTML
        embeds sit inside its body, not among the attachments, so they never
        get this far: parse_email only sees attachments.)"""
        if image_kind(self.content) is None or len(self.content) < MIN_PHOTO_BYTES:
            return False
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(self.content))
            width, height = image.size
            # Phones store most photos sideways and flag the rotation.
            if image.getexif().get(0x0112) in (5, 6, 7, 8):
                width, height = height, width
        except Exception:
            # Not something we can open: leave it for the upload path to
            # refuse with a reason, rather than guessing here.
            return True
        if min(width, height) < MIN_PHOTO_SHORT_SIDE:
            return False
        return width / height <= MAX_PHOTO_ASPECT and height / width <= MAX_TALL_PHOTO_ASPECT


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

    attachments = _attachments(message)

    return ParsedEmail(
        recipients=recipients,
        subject=message.get("subject"),
        message_id=message.get("message-id"),
        attachments=attachments,
    )


def _attachments(message: EmailMessage, depth: int = 0) -> list[EmailAttachment]:
    """What's attached to a message, including what's inside a message
    attached to it and inside a zip.

    "Forward as attachment" (how Outlook forwards several messages at once)
    puts the distributor's email, PDF and all, inside the one that arrives;
    and a week's invoices are often sent zipped. Both used to be turned away
    as "no PDF or photo attached"."""
    found: list[EmailAttachment] = []
    # A delivery-failure notice carries the message that couldn't be
    # delivered. That is something sent out coming back, not an invoice
    # sent in.
    bounce = _is_bounce(message)
    for part in message.iter_attachments():
        filename = part.get_filename() or "attachment"
        content_type = (part.get_content_type() or "").lower()
        inner = None
        if bounce and content_type in ("message/rfc822", "text/rfc822-headers", "message/delivery-status"):
            found.append(EmailAttachment(filename="a delivery-failure notice", content_type=content_type, content=b""))
            continue
        if content_type == "message/rfc822":
            parts = part.get_payload()
            inner = parts[0] if isinstance(parts, list) and parts else None
            encoding = str(part.get("content-transfer-encoding", "")).strip().lower()
            if isinstance(inner, EmailMessage) and not inner.keys() and encoding in ("base64", "quoted-printable"):
                # Some mail programs encode the attached message like any
                # other file, which the standard forbids and the parser
                # doesn't undo: the "message" is then one block of base64
                # with no headers, and the invoice in it was never found.
                inner = _decoded(inner, encoding) or inner
        elif filename.lower().endswith(".eml"):
            # Some mail programs attach it as a plain file.
            data = part.get_payload(decode=True)
            try:
                inner = message_from_bytes(data, policy=policy.default) if data else None
            except Exception:
                inner = None
        if isinstance(inner, EmailMessage):
            inside = _attachments(inner, depth + 1) if depth < MAX_FORWARD_DEPTH else []
            # With nothing in it, the message itself, so a rejection names it.
            found.extend(inside or [EmailAttachment(filename=filename, content_type=content_type, content=b"")])
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        attachment = EmailAttachment(
            filename=filename,
            content_type=content_type,
            content=payload,
            disposition=part.get_content_disposition(),
            content_id=part.get("content-id"),
        )
        if _is_zip(attachment):
            found.extend(_unzipped(attachment))
        elif _is_winmail(attachment):
            found.extend(_from_winmail(attachment))
        else:
            found.append(attachment)
    return found


def _decoded(message: EmailMessage, encoding: str) -> EmailMessage | None:
    """The message an attached message really is, once the transfer
    encoding it was wrongly given is undone; None if that isn't a message."""
    try:
        text = str(message.get_payload()).encode("ascii", "ignore")
        raw = base64.b64decode(text) if encoding == "base64" else quopri.decodestring(text)
        inner = message_from_bytes(raw, policy=policy.default)
    except Exception:
        return None
    return inner if isinstance(inner, EmailMessage) and inner.keys() else None


def _is_bounce(message: EmailMessage) -> bool:
    """Whether a message is a mail server's own notice (it couldn't deliver
    something, or someone is away) and not a person or a distributor
    writing in. Not every server sends the standard report: one arrived as
    an ordinary message from MAILER-DAEMON with the undelivered email
    attached, and the invoice in that was filed."""
    if message.get_content_type() == "multipart/report":
        return True
    senders = [address.lower() for _, address in getaddresses([str(v) for v in message.get_all("from", [])])]
    if any(address.split("@")[0] in _BOUNCE_SENDERS for address in senders):
        return True
    # An empty return path is how mail servers mark their own notices, so
    # that a notice never gets one back.
    if str(message.get("return-path", "")).strip() == "<>" or message.get("x-failed-recipients"):
        return True
    return str(message.get("auto-submitted", "")).strip().lower().startswith("auto-replied")


_TNEF_SIGNATURE = bytes.fromhex("789f3e22")


def _is_winmail(attachment: EmailAttachment) -> bool:
    """Outlook's wrapper: a message sent as rich text arrives, in other mail
    programs, as one attachment called winmail.dat with the real
    attachments inside it."""
    return (
        attachment.filename.lower() == "winmail.dat"
        or attachment.content_type in ("application/ms-tnef", "application/vnd.ms-tnef")
        or attachment.content.startswith(_TNEF_SIGNATURE)
    )


def _from_winmail(wrapper: EmailAttachment) -> list[EmailAttachment]:
    """The PDFs inside a winmail.dat, as if each had been attached. The
    wrapper stores its attachments as they are, so each PDF is there from
    its header to its end marker; nothing else in the format is read. With
    none in it, the wrapper itself, so a rejection names it."""
    data, pdfs = wrapper.content, []
    starts = [match.start() for match in re.finditer(rb"%PDF-", data)]
    index = 0
    while index < len(starts):
        start, taken = starts[index], None
        # To the last end marker before the next header. When that doesn't
        # open, the next header was inside this PDF (one with a PDF attached
        # to it), so on to the one after.
        for following in range(index + 1, len(starts) + 1):
            end = data.rfind(b"%%EOF", start, starts[following] if following < len(starts) else len(data))
            if end == -1:
                continue
            piece = data[start : end + len(b"%%EOF")] + b"\n"
            taken = taken or (piece, index + 1)  # the shortest, if none opens: the gate then says why
            if _opened(piece)[0] is None:
                taken = (piece, following)
                break
        if taken is None:
            index += 1
            continue
        pdfs.append(taken[0])
        index = taken[1]
    if not pdfs:
        return [EmailAttachment(filename=wrapper.filename, content_type=wrapper.content_type, content=b"")]
    return [
        EmailAttachment(filename=f"{wrapper.filename}-{number}.pdf", content_type="application/pdf", content=pdf)
        for number, pdf in enumerate(pdfs, 1)
    ]


def _is_zip(attachment: EmailAttachment) -> bool:
    # By name or type as well as by its bytes: Word and Excel files are zips
    # too, and the pictures inside those are logos, not invoices.
    labelled = attachment.filename.lower().endswith(".zip") or attachment.content_type in _ZIP_TYPES
    return labelled and attachment.content.startswith(b"PK")


class _ZipProblem(Exception):
    """Why a zip can't be taken in full."""


def _unzipped(archive: EmailAttachment) -> list[EmailAttachment]:
    """The PDFs and pictures in a zip, as if each had been attached,
    including those in a zip inside it. A zip that can't be taken in full (a
    password, a file too big, too many files) comes back with a `problem`,
    which turns the whole email away: taking forty invoices of sixty and
    saying nothing would be worse. With nothing usable in it and nothing
    wrong, the zip itself, so a rejection names it."""
    files: list[EmailAttachment] = []
    problem = None
    try:
        # Never trusting the sizes the zip claims: each file is read up to
        # the upload limit and no further, and all of them together (zips
        # inside it included) up to a few times that, so a small zip can't
        # unpack into gigabytes of memory.
        _take_from_zip(archive.content, files, [4 * settings.max_upload_bytes], depth=1)
    except _ZipProblem as exc:
        problem = str(exc)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, ValueError):
        problem = "it couldn't be opened"
    if problem:
        return [EmailAttachment(filename=archive.filename, content_type=archive.content_type, content=b"", problem=problem)]
    return files or [EmailAttachment(filename=archive.filename, content_type=archive.content_type, content=b"")]


def _take_from_zip(content: bytes, files: list[EmailAttachment], budget: list[int], depth: int) -> None:
    """Adds a zip's PDFs and pictures to `files`. `budget` is the bytes
    still allowed, shared with the zips inside it."""
    limit = settings.max_upload_bytes
    with zipfile.ZipFile(io.BytesIO(content)) as zipped:
        for info in zipped.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise _ZipProblem("it's password-protected")
            if budget[0] <= 0:
                raise _ZipProblem("there's too much in it")
            allowed = min(limit, budget[0])
            with zipped.open(info) as member:
                data = member.read(allowed + 1)
            budget[0] -= len(data)
            if len(data) > allowed:
                raise _ZipProblem("a file in it is too big" if allowed == limit else "there's too much in it")
            if info.filename.lower().endswith(".zip") and data.startswith(b"PK"):
                # A zip of the month holding a zip for each week.
                if depth >= MAX_ZIP_DEPTH:
                    raise _ZipProblem("it holds zips inside zips, too deep to open")
                _take_from_zip(data, files, budget, depth + 1)
            elif is_pdf_bytes(data) or image_kind(data) is not None:
                if len(files) >= MAX_ZIP_FILES:
                    raise _ZipProblem(f"it holds more than {MAX_ZIP_FILES} invoices; send them in smaller batches")
                files.append(EmailAttachment(filename=Path(info.filename).name, content_type="", content=data))


def find_tenant_for_recipients(db: Session, recipients: list[str]) -> Tenant | None:
    """First recipient address that maps to a tenant wins.

    Matched case-insensitively: addresses are case-insensitive in practice,
    and a tenant whose stored address differs only in case from what the mail
    server delivered would otherwise quarantine for no reason.

    A tag after a plus sign is ignored ("harbor-and-pine+sysco@..."): people
    give each distributor its own so they can tell who is sending what, and
    mail servers deliver it to the plain address. Addresses here are never
    made with a plus in them (inbox_address_for).
    """
    for address in recipients:
        local, _, domain = address.rpartition("@")
        for candidate in dict.fromkeys((address, f"{local.split('+')[0]}@{domain}")):
            tenant = db.scalar(select(Tenant).where(func.lower(Tenant.inbox_address) == candidate))
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

    for attachment in parsed.attachments:
        if attachment.problem:
            return _Rejected(f"attachment {attachment.filename!r} rejected: {attachment.problem}{context}", parsed, tenant)

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
    seen: set[str] = set()
    for attachment in routed.pdfs:
        # A file this location already has (uploaded, or forwarded before) is
        # skipped, as an upload of it is refused: it would be counted twice.
        digest = file_hash(attachment.content)
        already = None if digest in seen else db.scalar(
            select(Invoice.id).where(*same_file(tenant.id, digest)).limit(1)
        )
        if digest in seen or already is not None:
            audit.record(
                db,
                None,
                "invoice.duplicate_file_skipped",
                "invoice",
                already,
                tenant.id,
                filename=attachment.filename,
                message_id=parsed.message_id,
            )
            continue
        seen.add(digest)
        # One invoice per PDF: a distributor mailing a week's invoices as
        # several attachments is a single email but several invoices.
        invoice = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            file_sha256=digest,
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


def _duplicate_reason(invoice_ids: list[uuid.UUID] | None, dedupe_key: str, context: str) -> str:
    if invoice_ids is None:
        return f"already ingested (message-id {dedupe_key}){context}"
    return f"every attachment is a file this location already has{context}"


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
    if not invoice_ids:
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=_duplicate_reason(invoice_ids, routed.dedupe_key, routed.context),
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
    if not invoice_ids:  # another delivery of the same message won the race, or nothing new in it
        return IngestResult(
            source_name=source_name,
            status="duplicate",
            reason=_duplicate_reason(invoice_ids, routed.dedupe_key, context),
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
