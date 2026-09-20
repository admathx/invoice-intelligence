"""Phase 6 gate, per SPEC.md §10: correct tenant routing, multi-attachment
handling, graceful rejection of non-invoice attachments and unroutable
addresses.

The exit criterion is really one rule — nothing is silently dropped — so
every rejection path below asserts the email ended up in quarantine with a
readable reason, not just that no invoice was created.
"""
import io
import uuid
from email.message import EmailMessage

import pytest
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.db import bind_tenant
from app.ingest.email_stub import (
    QUARANTINE_DIRNAME,
    inbox_address_for,
    ingest_email_file,
    parse_email,
    scan_inbox,
)
from app.models import Invoice, Tenant
from app.models.enums import InvoiceSource, InvoiceStatus, VolumeTier


@pytest.fixture(autouse=True)
def no_real_queue(monkeypatch):
    """The worker does real extraction; these tests are about intake only."""
    enqueued: list[str] = []
    monkeypatch.setattr(
        "app.ingest.email_stub.invoice_queue.enqueue", lambda _fn, invoice_id: enqueued.append(invoice_id)
    )
    return enqueued


@pytest.fixture()
def inbox(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture()
def tenant(db_session):
    name = f"Email Tenant {uuid.uuid4().hex[:8]}"
    t = Tenant(
        name=name,
        inbox_address=inbox_address_for(name),
        metro="email-metro",
        volume_tier=VolumeTier.under_500k,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _pdf_bytes(text: str = "Emailed Invoice") -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def _write_eml(inbox, *, to: str, attachments: list[tuple[str, str, bytes]], name: str = "mail.eml", **headers):
    message = EmailMessage()
    message["From"] = "billing@sysco.example.com"
    message["To"] = to
    message["Subject"] = "Your weekly invoice"
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    message.set_content("Invoice attached.")
    for filename, subtype, data in attachments:
        maintype = "application" if subtype == "pdf" else "image"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    path = inbox / name
    path.write_bytes(message.as_bytes())
    return path


def _invoices_for(db, tenant) -> list[Invoice]:
    # Bound explicitly rather than relying on ingest having done it: on the
    # quarantine paths it never gets that far, and Invoice is TenantScoped.
    bind_tenant(db, tenant.id)
    return list(db.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id)))


# --- routing ---------------------------------------------------------------


def test_routes_to_the_tenant_that_owns_the_address(db_session, inbox, tenant, no_real_queue):
    path = _write_eml(inbox, to=tenant.inbox_address, attachments=[("invoice.pdf", "pdf", _pdf_bytes())])

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "ingested", result.reason
    assert result.tenant_id == tenant.id
    invoices = _invoices_for(db_session, tenant)
    assert len(invoices) == 1
    assert invoices[0].source == InvoiceSource.email
    assert invoices[0].status == InvoiceStatus.received
    assert invoices[0].original_file_uri.startswith("file://")
    assert no_real_queue == [str(invoices[0].id)]


def test_address_matching_is_case_insensitive(db_session, inbox, tenant):
    path = _write_eml(
        inbox, to=tenant.inbox_address.upper(), attachments=[("invoice.pdf", "pdf", _pdf_bytes())]
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "ingested", result.reason
    assert result.tenant_id == tenant.id


def test_routes_on_delivered_to_when_the_message_was_forwarded(db_session, inbox, tenant):
    # A restaurant forwarding a distributor's invoice leaves its own address
    # in Delivered-To while `To` becomes whoever they forwarded it to.
    path = _write_eml(
        inbox,
        to="someone-else@example.com",
        delivered_to=tenant.inbox_address,
        attachments=[("invoice.pdf", "pdf", _pdf_bytes())],
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "ingested", result.reason
    assert result.tenant_id == tenant.id


def test_unroutable_address_is_quarantined_not_dropped(db_session, inbox):
    path = _write_eml(
        inbox, to="nobody@invoices.example.com", attachments=[("invoice.pdf", "pdf", _pdf_bytes())]
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "quarantined"
    assert "no tenant" in result.reason
    # The evidence survives, with a readable explanation beside it.
    assert not path.exists()
    quarantined = inbox / QUARANTINE_DIRNAME / "mail.eml"
    assert quarantined.exists()
    assert "no tenant" in quarantined.with_suffix(".eml.reason.txt").read_text()


# --- attachments -----------------------------------------------------------


def test_multiple_pdfs_become_multiple_invoices(db_session, inbox, tenant, no_real_queue):
    path = _write_eml(
        inbox,
        to=tenant.inbox_address,
        attachments=[
            ("invoice-a.pdf", "pdf", _pdf_bytes("Invoice A")),
            ("invoice-b.pdf", "pdf", _pdf_bytes("Invoice B")),
        ],
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "ingested", result.reason
    assert len(result.invoice_ids) == 2
    assert len(_invoices_for(db_session, tenant)) == 2
    assert len(no_real_queue) == 2
    # Each invoice got its own stored file, not a shared/overwritten one.
    uris = {inv.original_file_uri for inv in _invoices_for(db_session, tenant)}
    assert len(uris) == 2


def test_non_invoice_attachments_are_ignored_alongside_a_pdf(db_session, inbox, tenant):
    path = _write_eml(
        inbox,
        to=tenant.inbox_address,
        attachments=[
            ("signature-logo.png", "png", b"\x89PNG\r\n\x1a\nnot-a-real-png"),
            ("invoice.pdf", "pdf", _pdf_bytes()),
        ],
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "ingested", result.reason
    assert len(result.invoice_ids) == 1


def test_email_with_no_pdf_is_quarantined(db_session, inbox, tenant):
    path = _write_eml(
        inbox,
        to=tenant.inbox_address,
        attachments=[("signature-logo.png", "png", b"\x89PNG\r\n\x1a\nnope")],
    )

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "quarantined"
    assert "no PDF attachment" in result.reason
    assert "signature-logo.png" in result.reason
    assert _invoices_for(db_session, tenant) == []


def test_malformed_email_is_quarantined_rather_than_crashing(db_session, inbox):
    path = inbox / "garbage.eml"
    path.write_bytes(b"\x00\x01 this is not a valid message at all")

    result = ingest_email_file(db_session, path, inbox)

    assert result.status == "quarantined"
    assert not path.exists()
    assert (inbox / QUARANTINE_DIRNAME / "garbage.eml").exists()


# --- the scan loop ---------------------------------------------------------


def test_scan_processes_every_email_and_isolates_failures(db_session, inbox, tenant):
    _write_eml(inbox, to=tenant.inbox_address, attachments=[("a.pdf", "pdf", _pdf_bytes())], name="good.eml")
    _write_eml(inbox, to="nobody@invoices.example.com", attachments=[("b.pdf", "pdf", _pdf_bytes())], name="bad.eml")

    results = scan_inbox(db_session, inbox)

    by_name = {r.source_name: r for r in results}
    assert by_name["good.eml"].status == "ingested"
    assert by_name["bad.eml"].status == "quarantined"
    # The inbox itself is left empty: every email moved to processed/ or
    # quarantine/, so a rerun can't double-ingest anything.
    assert list(inbox.glob("*.eml")) == []


def test_reprocessing_the_same_filename_does_not_clobber_the_first(db_session, inbox, tenant):
    for _ in range(2):
        _write_eml(
            inbox, to=tenant.inbox_address, attachments=[("a.pdf", "pdf", _pdf_bytes())], name="same.eml"
        )
        scan_inbox(db_session, inbox)

    processed = list((inbox / "processed").glob("*.eml"))
    assert len(processed) == 2, "second email with the same filename overwrote the first"


# --- parsing ---------------------------------------------------------------


def test_parse_collects_recipients_from_every_relevant_header():
    message = EmailMessage()
    message["To"] = "a@example.com"
    message["Cc"] = "B@Example.com"
    message["Delivered-To"] = "c@example.com"
    message.set_content("hi")

    parsed = parse_email(message.as_bytes())

    assert parsed.recipients == ["a@example.com", "b@example.com", "c@example.com"]
