"""File storage (app/storage.py): the local directory and S3-compatible
buckets behave the same, and the whole upload -> render -> review-screen path
works on a bucket, not just on the disk it was built against."""
import io
import uuid

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from reportlab.pdfgen import canvas
from sqlalchemy import delete, select, update

from app.config import settings
from app.db import SessionLocal
from app.extract.client import FakeExtractorClient
from app.main import app
from app.models import AuditEvent, Invoice, InvoiceLineItem, PriceAlert, PriceObservation, Tenant
from app.models.enums import InvoiceStatus, VolumeTier
from app.storage import LocalStorage, S3Storage, StorageError, get_storage, read_uri
from app.workers.tasks import process_invoice

BUCKET = "invoice-intelligence-test"


@pytest.fixture()
def s3(monkeypatch):
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(settings, "storage_backend", "s3")
        monkeypatch.setattr(settings, "s3_bucket", BUCKET)
        monkeypatch.setattr(settings, "s3_prefix", "test/")
        monkeypatch.setattr(settings, "s3_region", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        get_storage.cache_clear()
        try:
            yield get_storage()
        finally:
            get_storage.cache_clear()


@pytest.fixture(params=["local", "s3"])
def storage(request, tmp_path):
    if request.param == "local":
        yield LocalStorage(tmp_path)
    else:
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            yield S3Storage(BUCKET, prefix="p/", region="us-east-1")


def test_put_get_list_and_delete_behave_the_same_on_both_backends(storage):
    storage.put("renders/a/page_002.png", b"two")
    storage.put("renders/a/page_001.png", b"one")
    storage.put("renders/b/page_001.png", b"other")
    assert storage.get("renders/a/page_001.png") == b"one"
    assert storage.get("renders/a/missing.png") is None
    assert storage.list("renders/a/") == ["renders/a/page_001.png", "renders/a/page_002.png"]

    storage.put("renders/a/page_001.png", b"replaced")
    assert storage.get("renders/a/page_001.png") == b"replaced"

    storage.delete_prefix("renders/a/")
    assert storage.list("renders/a/") == []
    assert storage.get("renders/b/page_001.png") == b"other"


def test_keys_cannot_escape_the_storage_root(storage):
    for key in ("../secret", "renders/../../etc/passwd", "/etc/passwd"):
        with pytest.raises(StorageError):
            storage.put(key, b"x")


def test_read_uri_follows_the_scheme_the_row_was_written_with(tmp_path, s3):
    # A row written while the deployment used local disk still opens after a
    # switch to S3, as long as the file is still there.
    legacy = tmp_path / "old.pdf"
    legacy.write_bytes(b"%PDF-legacy")
    assert read_uri(legacy.as_uri()) == b"%PDF-legacy"

    uri = s3.put("originals/x.pdf", b"%PDF-new")
    assert uri == f"s3://{BUCKET}/test/originals/x.pdf"
    assert read_uri(uri) == b"%PDF-new"
    with pytest.raises(StorageError):
        read_uri("s3://some-other-bucket/x.pdf")
    with pytest.raises(StorageError):
        read_uri((tmp_path / "gone.pdf").as_uri())


def _two_page_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for n in (1, 2):
        c.drawString(72, 720, f"Invoice page {n}")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_upload_render_and_review_screen_work_on_a_bucket(s3, monkeypatch):
    monkeypatch.setattr("app.api.invoices.queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())
    db = SessionLocal()
    tenant = Tenant(name=f"Bucket Tenant {uuid.uuid4().hex[:6]}", metro="bucket-metro", volume_tier=VolumeTier.under_500k)
    db.add(tenant)
    db.commit()
    tenant_id = tenant.id
    try:
        client = TestClient(app)
        resp = client.post(
            "/invoices", params={"tenant_id": str(tenant_id)}, files={"file": ("x.pdf", _two_page_pdf(), "application/pdf")}
        )
        assert resp.status_code == 201, resp.text
        invoice_id = resp.json()["id"]
        process_invoice(invoice_id)

        # Nothing landed on local disk; everything is in the bucket.
        keys = s3.list("")
        assert keys == [
            f"originals/{invoice_id}.pdf",
            f"renders/{invoice_id}/page_001.png",
            f"renders/{invoice_id}/page_002.png",
        ]
        detail = client.get(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant_id)}).json()
        assert detail["status"] == "extracted"
        assert len(detail["page_image_urls"]) == 2
        page = client.get(detail["page_image_urls"][1])
        assert page.status_code == 200 and page.content.startswith(b"\x89PNG")

        # A retry re-renders from the bucket and replaces, not appends.
        db.execute(update(Invoice).where(Invoice.id == uuid.UUID(invoice_id)).values(status=InvoiceStatus.failed))
        db.commit()
        process_invoice(invoice_id)
        assert len(s3.list(f"renders/{invoice_id}/")) == 2
    finally:
        db.rollback()
        db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
        lines = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id == tenant_id)
        db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(lines)))
        db.execute(delete(PriceAlert).where(PriceAlert.tenant_id == tenant_id))
        db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id == tenant_id))
        db.execute(delete(Invoice).where(Invoice.tenant_id == tenant_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        db.commit()
        db.close()
