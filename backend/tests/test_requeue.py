"""Invoices whose extraction job was lost are picked up again (app/requeue.py)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import requeue
from app.main import app
from app.models import AuditEvent, Invoice
from app.models.enums import InvoiceSource, InvoiceStatus

from test_review_api import _make_test_pdf, db_session, tenant  # noqa: F401

LATER = datetime.now(timezone.utc) + timedelta(minutes=10)


@pytest.fixture()
def queued(monkeypatch):
    sent = []
    monkeypatch.setattr("app.queue.enqueue_extraction", lambda invoice_id: sent.append(invoice_id))
    monkeypatch.setattr(requeue, "_job_is_alive", lambda invoice_id: False)
    monkeypatch.setattr(requeue.ops, "alert", lambda *a, **k: True)
    return sent


def _stuck(db, tenant, status=InvoiceStatus.extracting) -> uuid.UUID:
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant.id, source=InvoiceSource.upload, original_file_uri="file:///dev/null", status=status
    )
    db.add(invoice)
    db.commit()
    return invoice.id


def _mine(ids, sent):
    return [i for i in sent if i in ids]


def test_an_invoice_whose_job_was_lost_is_queued_again(db_session, tenant, queued):
    stuck = _stuck(db_session, tenant)
    requeue.requeue_stalled(db_session, LATER, tenant_ids=[tenant.id])
    assert _mine([stuck], queued) == [stuck]
    assert db_session.scalar(select(AuditEvent.details).where(AuditEvent.entity_id == stuck, AuditEvent.action == "invoice.requeued")) == {"attempt": 1}


def test_one_still_being_worked_on_or_just_uploaded_is_left_alone(db_session, tenant, queued, monkeypatch):
    working = _stuck(db_session, tenant)
    monkeypatch.setattr(requeue, "_job_is_alive", lambda invoice_id: invoice_id == working)
    fresh = _stuck(db_session, tenant, InvoiceStatus.received)
    requeue.requeue_stalled(db_session, datetime.now(timezone.utc), tenant_ids=[tenant.id])  # within the grace period for `fresh`
    assert _mine([working, fresh], queued) == []


def test_after_a_few_tries_it_stops_and_says_so(db_session, tenant, queued):
    stuck = _stuck(db_session, tenant)
    for _ in range(requeue.MAX_REQUEUES):
        requeue.requeue_stalled(db_session, LATER, tenant_ids=[tenant.id])
    assert _mine([stuck], queued) == [stuck] * requeue.MAX_REQUEUES
    requeue.requeue_stalled(db_session, LATER, tenant_ids=[tenant.id])
    db_session.expire_all()
    assert db_session.get(Invoice, stuck).status == InvoiceStatus.failed
    assert len(_mine([stuck], queued)) == requeue.MAX_REQUEUES


def test_an_upload_survives_the_queue_being_down(db_session, tenant, monkeypatch):
    def down(*a, **k):
        raise ConnectionError("redis is down")

    monkeypatch.setattr("app.queue.invoice_queue.enqueue", down)
    monkeypatch.setattr("app.api.invoices.ops.alert", lambda *a, **k: True)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("test.pdf", _make_test_pdf(), "application/pdf")}
    )
    assert resp.status_code == 201, "saved, and picked up later: not a failure to retry (and duplicate)"
