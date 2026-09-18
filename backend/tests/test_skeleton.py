"""Phase 0 gate: upload -> job -> rows -> API read, per SPEC.md §10.

Runs the worker task synchronously (no live Redis/RQ needed) so this test works
without Docker. `make smoke` separately exercises the real async queue end to end.
"""
import io
import uuid

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app
from app.models import Tenant
from app.models.enums import InvoiceStatus, VolumeTier
from app.workers.tasks import process_invoice


def _make_test_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Test Invoice")
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture()
def db_session():
    with engine.connect() as conn:
        assert conn.scalar(text("select 1")) == 1
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def tenant(db_session):
    t = Tenant(name="Test Tenant", metro="test-metro", volume_tier=VolumeTier.under_500k)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    yield t


@pytest.fixture()
def other_tenant(db_session):
    t = Tenant(name="Other Tenant", metro="other-metro", volume_tier=VolumeTier.under_500k)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    yield t


def test_upload_job_rows_api_read(tenant, other_tenant, monkeypatch):
    # Don't push onto the real Redis queue: if a dev worker (`make up`) is also
    # running against the same Redis, it races this test's own synchronous call
    # to process_invoice below and double-processes the invoice.
    monkeypatch.setattr("app.api.invoices.queue.enqueue", lambda *a, **k: None)

    client = TestClient(app)

    pdf_bytes = _make_test_pdf()
    resp = client.post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    invoice_id = resp.json()["id"]
    assert resp.json()["status"] == InvoiceStatus.received.value

    # Run the job synchronously instead of via the RQ queue.
    process_invoice(invoice_id)

    resp = client.get(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == InvoiceStatus.extracted.value
    assert len(body["line_items"]) == 2
    assert body["line_items"][0]["raw_description"]

    # Tenant isolation: another tenant guessing/knowing this invoice's id must
    # not be able to read it, even though the row exists (app.db.TenantScoped).
    resp = client.get(f"/invoices/{invoice_id}", params={"tenant_id": str(other_tenant.id)})
    assert resp.status_code == 404, resp.text
