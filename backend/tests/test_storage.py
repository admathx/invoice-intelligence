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
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
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


def test_a_directory_with_spaces_can_read_back_what_it_stored(tmp_path):
    storage = LocalStorage(tmp_path / "My Projects" / "uploads ü")
    uri = storage.put("originals/x.pdf", b"%PDF-spaced")
    assert "%20" in uri  # as_uri encodes; read_uri has to decode
    assert read_uri(uri) == b"%PDF-spaced"


def test_delete_removes_one_key(storage):
    storage.put("originals/a.pdf", b"a")
    storage.put("originals/b.pdf", b"b")
    storage.delete("originals/a.pdf")
    storage.delete("originals/missing.pdf")  # no error
    assert storage.list("originals/") == ["originals/b.pdf"]


def test_a_failed_database_write_leaves_no_stored_original(monkeypatch, isolated_storage):
    """The file is stored before the row commits; when the write fails after
    that, the file goes too, instead of sitting in storage referenced by
    nothing."""
    db = SessionLocal()
    tenant = Tenant(name=f"Orphan Tenant {uuid.uuid4().hex[:6]}", metro="orphan-metro", volume_tier=VolumeTier.under_500k)
    db.add(tenant)
    db.commit()
    tenant_id = tenant.id

    def database_went_away(*args, **kwargs):
        raise RuntimeError("database went away")

    try:
        # Fails between storing the file and committing the row.
        monkeypatch.setattr("app.api.invoices.audit.record", database_went_away)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/invoices", params={"tenant_id": str(tenant_id)}, files={"file": ("x.pdf", _two_page_pdf(), "application/pdf")}
        )
        assert resp.status_code == 500
        assert isolated_storage.list("originals/") == []
        assert db.scalar(
            select(Invoice.id).where(Invoice.tenant_id == tenant_id).execution_options(tenant_scope_bypass=True)
        ) is None
    finally:
        monkeypatch.undo()
        db.execute(delete(Invoice).where(Invoice.tenant_id == tenant_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        db.commit()
        db.close()


def test_switching_to_a_bucket_keeps_existing_invoices_whole(tmp_path, s3):
    """Rendered pages are looked up in the current backend only, so after a
    switch an existing invoice lost its page images until they were moved;
    scripts/migrate_storage.py moves originals and pages, and re-running it
    moves nothing twice."""
    from scripts.migrate_storage import migrate

    from app.storage import page_names, render_key

    old = LocalStorage(tmp_path / "old-uploads")
    db = SessionLocal()
    tenant = Tenant(name=f"Migrating Tenant {uuid.uuid4().hex[:6]}", metro="m", volume_tier=VolumeTier.under_500k)
    db.add(tenant)
    db.commit()
    tenant_id = tenant.id
    invoice_id = uuid.uuid4()
    try:
        db.add(
            Invoice(
                id=invoice_id,
                tenant_id=tenant_id,
                source="upload",
                status=InvoiceStatus.extracted,
                original_file_uri=old.put(f"originals/{invoice_id}.pdf", b"%PDF-old"),
            )
        )
        db.commit()
        old.put(render_key(invoice_id, "page_001.png"), b"\x89PNG one")
        old.put(render_key(invoice_id, "page_002.png"), b"\x89PNG two")
        assert page_names(invoice_id) == []  # the bucket has nothing yet: the bug

        # Restricted to this test's invoice: the dev database has others.
        first = migrate(db, old, s3, only=[invoice_id])
        assert (first.originals_copied, first.pages_copied) == (1, 2)
        assert page_names(invoice_id) == ["page_001.png", "page_002.png"]
        uri = db.scalar(select(Invoice.original_file_uri).where(Invoice.id == invoice_id).execution_options(tenant_scope_bypass=True))
        assert uri.startswith(f"s3://{BUCKET}/") and read_uri(uri) == b"%PDF-old"

        again = migrate(db, old, s3, only=[invoice_id])
        assert (again.originals_copied, again.pages_copied, again.originals_already_there) == (0, 0, 1)
    finally:
        db.rollback()
        db.execute(delete(Invoice).where(Invoice.id == invoice_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        db.commit()
        db.close()


def test_put_file_stores_a_file_without_reading_it_whole(storage, tmp_path):
    source = tmp_path / "invoice-20260901-0700.dump"
    source.write_bytes(b"PGDMP" + b"x" * 10_000)
    storage.put_file("backups/invoice-20260901-0700.dump", source)
    assert storage.get("backups/invoice-20260901-0700.dump") == source.read_bytes()
    assert storage.list("backups/") == ["backups/invoice-20260901-0700.dump"]
