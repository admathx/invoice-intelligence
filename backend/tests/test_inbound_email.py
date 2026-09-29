"""The inbound email webhook (app/api/inbound.py): what a mail provider posts
becomes invoices for the right location, exactly once, and anything that
can't is kept with a reason rather than dropped."""
import base64
import io
import json
import threading
import uuid
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import delete, select

from app.config import settings
from app.db import SessionLocal
from app.ingest.email_stub import inbox_address_for
from app.main import app
from app.models import AuditEvent, Invoice, Tenant
from app.models.enums import InvoiceSource, VolumeTier

SECRET = "inbound-test-secret-" + uuid.uuid4().hex
BEARER = {"Authorization": f"Bearer {SECRET}"}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "inbound_email_secret", SECRET)
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)


@pytest.fixture()
def tenant():
    """Committed, since the endpoint opens its own session; removed after,
    with its invoices and their history."""
    db = SessionLocal()
    name = f"Inbound Tenant {uuid.uuid4().hex[:8]}"
    t = Tenant(name=name, inbox_address=inbox_address_for(name), metro="inbound-metro", volume_tier=VolumeTier.under_500k)
    db.add(t)
    db.commit()
    tenant_id, address = t.id, t.inbox_address
    try:
        yield t
    finally:
        db.rollback()
        db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
        db.execute(delete(Invoice).where(Invoice.tenant_id == tenant_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        db.execute(delete(AuditEvent).where(AuditEvent.action == "email.rejected", AuditEvent.tenant_id.is_(None)))
        db.commit()
        db.close()
    assert address  # keep the linter honest about the captured value


def _pdf(padding: int = 0) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Emailed invoice")
    c.showPage()
    c.save()
    # Trailing bytes after %%EOF are ignored by PDF readers: an easy way to
    # make a realistically large attachment.
    return buf.getvalue() + b"\n%" + b"x" * padding


def _email(to: str, *, pdfs: int = 1, padding: int = 0, message_id: str | None = None) -> bytes:
    message = EmailMessage()
    message["From"] = "billing@sysco.example.com"
    message["To"] = to
    message["Subject"] = "Weekly invoice"
    message["Message-ID"] = message_id or f"<{uuid.uuid4().hex}@sysco.example.com>"
    message.set_content("Invoice attached.")
    for n in range(pdfs):
        message.add_attachment(_pdf(padding), maintype="application", subtype="pdf", filename=f"inv-{n}.pdf")
    return message.as_bytes()


def _invoices(tenant_id) -> list[Invoice]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(Invoice).where(Invoice.tenant_id == tenant_id).execution_options(tenant_scope_bypass=True)
            )
        )
    finally:
        db.close()


def _post(content: bytes, content_type: str = "message/rfc822", headers=BEARER):
    return TestClient(app).post("/inbound/email", content=content, headers={**headers, "Content-Type": content_type})


def test_off_unless_a_secret_is_configured(monkeypatch, tenant):
    monkeypatch.setattr(settings, "inbound_email_secret", "")
    assert _post(_email(tenant.inbox_address)).status_code == 404


def test_refuses_the_wrong_secret_and_accepts_basic_or_bearer(tenant):
    assert _post(_email(tenant.inbox_address), headers={}).status_code == 401
    assert _post(_email(tenant.inbox_address), headers={"Authorization": "Bearer nope"}).status_code == 401
    basic = base64.b64encode(f"inbound:{SECRET}".encode()).decode()
    assert _post(_email(tenant.inbox_address), headers={"Authorization": f"Basic {basic}"}).status_code == 200
    assert _post(_email(tenant.inbox_address)).status_code == 200


def test_a_raw_message_becomes_one_invoice_per_pdf(tenant):
    resp = _post(_email(tenant.inbox_address, pdfs=2))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ingested" and len(body["invoice_ids"]) == 2
    invoices = _invoices(tenant.id)
    assert {str(i.id) for i in invoices} == set(body["invoice_ids"])
    assert all(i.source == InvoiceSource.email for i in invoices)


def test_sendgrid_raw_form_field_of_several_megabytes(tenant):
    raw = _email(tenant.inbox_address, padding=3 * 1024 * 1024)
    boundary = "xYzZY"
    form = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"to\"\r\n\r\n{tenant.inbox_address}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"email\"\r\n\r\n"
    ).encode() + raw + f"\r\n--{boundary}--\r\n".encode()
    resp = _post(form, f"multipart/form-data; boundary={boundary}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ingested"


def test_mailgun_mime_field_and_postmark_json(tenant):
    boundary = "mg"
    form = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"body-mime\"\r\n\r\n".encode()
        + _email(tenant.inbox_address)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    assert _post(form, f"multipart/form-data; boundary={boundary}").json()["status"] == "ingested"

    postmark = json.dumps({"From": "x", "RawEmail": _email(tenant.inbox_address).decode()}).encode()
    assert _post(postmark, "application/json").json()["status"] == "ingested"
    assert len(_invoices(tenant.id)) == 2

    no_raw = json.dumps({"From": "x", "TextBody": "hi"}).encode()
    assert _post(no_raw, "application/json").status_code == 422


def test_a_provider_retry_is_a_duplicate_not_a_second_invoice(tenant):
    message = _email(tenant.inbox_address, message_id="<retry-1@sysco.example.com>")
    assert _post(message).json()["status"] == "ingested"
    again = _post(message).json()
    assert again["status"] == "duplicate" and again["invoice_ids"] == []
    assert len(_invoices(tenant.id)) == 1


def test_simultaneous_deliveries_of_one_message_make_one_invoice(tenant):
    message = _email(tenant.inbox_address, message_id="<race-1@sysco.example.com>")
    statuses: list[str] = []
    threads = [threading.Thread(target=lambda: statuses.append(_post(message).json()["status"])) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(statuses) == ["duplicate", "duplicate", "duplicate", "ingested"]
    assert len(_invoices(tenant.id)) == 1


def test_an_unroutable_email_is_kept_and_logged_not_dropped(tenant, isolated_storage):
    resp = _post(_email("nobody@invoices.example.com"))
    assert resp.status_code == 200  # a retry would never succeed, so don't ask for one
    assert resp.json()["status"] == "rejected" and "no tenant" in resp.json()["reason"]

    db = SessionLocal()
    try:
        event = db.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "email.rejected", AuditEvent.details["recipients"][0].astext == "nobody@invoices.example.com")
            .order_by(AuditEvent.occurred_at.desc())
        )
    finally:
        db.close()
    assert event is not None and event.tenant_id is None
    stored = isolated_storage.get(event.details["stored_at"])
    assert stored is not None and b"nobody@invoices.example.com" in stored


def test_a_rejection_for_a_known_location_shows_in_its_activity(tenant):
    message = EmailMessage()
    message["To"] = tenant.inbox_address
    message["Subject"] = "Statement"
    message.set_content("No attachment, just a note.")
    resp = _post(message.as_bytes())
    assert resp.json()["status"] == "rejected"

    db = SessionLocal()
    try:
        event = db.scalar(select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "email.rejected"))
    finally:
        db.close()
    assert event is not None and "no PDF or photo of an invoice attached" in event.details["reason"]
    assert event.details["subject"] == "Statement"


def test_an_oversized_email_is_refused_before_it_is_read(monkeypatch, tenant):
    monkeypatch.setattr(settings, "max_inbound_email_bytes", 10_000)
    resp = _post(_email(tenant.inbox_address, padding=20_000))
    assert resp.status_code == 413
    assert _invoices(tenant.id) == []


def test_extraction_is_queued_with_a_time_limit_that_fits_a_large_invoice(monkeypatch, tenant):
    calls = []
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: calls.append(k))
    assert _post(_email(tenant.inbox_address)).json()["status"] == "ingested"
    # RQ's own default (180 s) killed long extractions mid-stream.
    assert calls == [{"job_timeout": settings.extraction_job_timeout_seconds}]
    assert settings.extraction_job_timeout_seconds >= 900


def test_a_retried_email_without_a_message_id_is_still_a_duplicate(tenant):
    """Scanners and some relays send no Message-ID. A provider's retry of one
    is the same bytes, so the message's own hash identifies it."""
    message = EmailMessage()
    message["To"] = tenant.inbox_address
    message["Subject"] = "Scan from copier"
    message.add_attachment(_pdf(), maintype="application", subtype="pdf", filename="scan.pdf")
    raw = message.as_bytes()
    assert "Message-ID" not in message

    assert _post(raw).json()["status"] == "ingested"
    assert _post(raw).json()["status"] == "duplicate"
    # A different scan (different bytes) is a different message.
    other = EmailMessage()
    other["To"] = tenant.inbox_address
    other["Subject"] = "Scan from copier"
    other.add_attachment(_pdf(padding=10), maintype="application", subtype="pdf", filename="scan.pdf")
    assert _post(other.as_bytes()).json()["status"] == "ingested"
    assert len(_invoices(tenant.id)) == 2
