"""Covers the two gaps a full-codebase review found in the live review path:

1. The real worker pipeline (app/workers/tasks.py) has to write a
   price_observations row for auto-matched lines. Before, only the review
   endpoints did, and they only ever act on `pending` lines — so the ~99%
   of real lines that auto-match never reached analytics at all.
2. There was no way to send an already-resolved line back for review, so a
   wrong auto-match was permanent and kept skewing benchmarks forever.
"""
import io
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import select

from sqlalchemy import delete

from app.db import SessionLocal, bind_tenant
from app.extract.client import FAKE_PAYLOAD, ExtractionFailedError, FakeExtractorClient
from app.main import app
from app.models import (
    Account,
    CanonicalSku,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceAlert,
    PriceObservation,
    SkuAlias,
    Tenant,
)
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier
from app.workers.tasks import process_invoice


def _make_test_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Review API Test Invoice")
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture()
def db_session():
    """Overrides conftest.py's rollback-based session for this module only.

    These tests drive the real HTTP app (and the worker), both of which open
    their own SessionLocal — so data has to be genuinely committed to be
    visible to them. Everything created is deleted again at teardown, so this
    still doesn't leave rows behind in the dev database.
    """
    session = SessionLocal()
    created: dict[str, list] = {"tenants": [], "distributors": [], "skus": [], "aliases": []}
    session.info["_created"] = created
    try:
        yield session
    finally:
        session.rollback()
        tenant_ids = created["tenants"]
        if tenant_ids:
            line_ids = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id.in_(tenant_ids))
            session.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
            session.execute(delete(PriceObservation).where(PriceObservation.tenant_id.in_(tenant_ids)))
            session.execute(delete(PriceAlert).where(PriceAlert.tenant_id.in_(tenant_ids)))
            # Aliases the confirm/correct endpoints wrote on this tenant's
            # behalf. Not in created["aliases"] — the API made them, not the
            # test — and since sku_aliases.tenant_id became a real FK, missing
            # them makes the Tenant delete below fail instead of just leaking.
            session.execute(delete(SkuAlias).where(SkuAlias.tenant_id.in_(tenant_ids)))
            session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(tenant_ids)))
            session.execute(delete(Invoice).where(Invoice.tenant_id.in_(tenant_ids)))
            session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        if created["distributors"]:
            session.execute(delete(SkuAlias).where(SkuAlias.distributor_id.in_(created["distributors"])))
            session.execute(delete(Distributor).where(Distributor.id.in_(created["distributors"])))
        if created["aliases"]:
            session.execute(delete(SkuAlias).where(SkuAlias.id.in_(created["aliases"])))
        if created["skus"]:
            session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(created["skus"])))
        session.commit()
        session.close()


@pytest.fixture()
def tenant(db_session):
    t = Tenant(name=f"Review API Tenant {uuid.uuid4().hex[:8]}", metro="review-api-metro", volume_tier=VolumeTier.under_500k)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    # The test session queries TenantScoped models directly; app/db.py's guard
    # (correctly) refuses those unless a tenant is bound.
    bind_tenant(db_session, t.id)
    db_session.info["_created"]["tenants"].append(t.id)
    return t


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Review API Distributor", slug=f"review-api-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    db_session.info["_created"]["distributors"].append(d.id)
    return d


@pytest.fixture()
def canonical_sku(db_session):
    sku = CanonicalSku(name=f"Review API SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    db_session.info["_created"]["skus"].append(sku.id)
    return sku


def _auto_matched_line(db, tenant, distributor, sku) -> InvoiceLineItem:
    """A line in the state the matcher leaves an auto-match in."""
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id,
        invoice_date=__import__("datetime").date(2026, 5, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    db.add(invoice)
    line = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        line_number=1,
        raw_description="AUTO MATCHED LINE",
        raw_sku="AUTO-1",
        raw_pack_size="1 LB",
        quantity=Decimal("1"),
        unit_price=Decimal("7.00"),
        extended_price=Decimal("7.00"),
        uom="LB",
        canonical_sku_id=sku.id,
        normalized_qty_base=Decimal("1"),
        normalized_unit_price=Decimal("7.00"),
        base_uom=sku.base_uom,
        match_confidence=Decimal("0.97"),
        review_status=ReviewStatus.auto,
    )
    db.add(line)
    db.commit()
    return line


def _pin_fake_line_to_auto(db_session, tenant) -> None:
    """Pin the fake extractor's mozzarella line to the auto tier deterministically.

    A tenant-owned alias makes match_line_item short-circuit with confidence
    1.0 / review_status=auto, rather than depending on where the embedding
    matcher happens to score the fake extractor's canned descriptions. Owned
    by this tenant rather than left NULL: a NULL alias is the system-curated
    tier, trusted everywhere without a second confirmation; attributing it
    exercises the path a real correction takes (match_by_alias rule 1).
    """
    sysco = db_session.scalar(select(Distributor).where(Distributor.slug == "sysco"))
    sku = db_session.scalar(select(CanonicalSku).where(CanonicalSku.name == "Mozzarella Shredded Whole Milk"))
    assert sysco is not None and sku is not None, "catalog/distributors not seeded — run `make seed`"
    db_session.add(
        SkuAlias(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=sysco.id,
            raw_description="MOZZ SHRD WHL MLK 4/5 LB",
            raw_sku="4001122",
            pack_size="4/5 LB",
        )
    )
    db_session.commit()


def _upload(tenant) -> uuid.UUID:
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("test.pdf", _make_test_pdf(), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def _line_ids(db_session, invoice_id) -> list[uuid.UUID]:
    return list(db_session.scalars(select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice_id)))


def _observations_for(db_session, invoice_id) -> list[PriceObservation]:
    return list(
        db_session.scalars(
            select(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(_line_ids(db_session, invoice_id)))
        )
    )


class _FixedExtractor:
    """Returns a given payload, or raises a given error, like the real client."""

    def __init__(self, payload=None, error: Exception | None = None) -> None:
        self.payload, self.error = payload, error

    def extract(self, page_image_paths):
        if self.error is not None:
            raise self.error
        return self.payload, 0.0


def test_worker_writes_price_observations_for_auto_matched_lines(db_session, tenant, monkeypatch):
    """The whole analytics stack reads exclusively from price_observations —
    if the live pipeline doesn't write them, real invoices never show up in
    price creep, benchmarks, or negotiation sheets.
    """
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())

    _pin_fake_line_to_auto(db_session, tenant)
    invoice_id = _upload(tenant)

    process_invoice(str(invoice_id))

    line_ids = list(
        db_session.scalars(select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice_id))
    )
    auto_line_ids = list(
        db_session.scalars(
            select(InvoiceLineItem.id).where(
                InvoiceLineItem.invoice_id == invoice_id,
                InvoiceLineItem.review_status == ReviewStatus.auto,
                InvoiceLineItem.normalized_unit_price.is_not(None),
            )
        )
    )
    observations = list(
        db_session.scalars(select(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
    )

    assert auto_line_ids, "fixture produced no auto-matched lines to assert on"
    assert {o.invoice_line_item_id for o in observations} == set(auto_line_ids)
    for observation in observations:
        assert observation.tenant_id == tenant.id
        assert observation.metro == tenant.metro


def test_reopen_sends_a_resolved_line_back_to_the_queue(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    db_session.add(
        PriceObservation(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=canonical_sku.id,
            distributor_id=distributor.id,
            observed_on=__import__("datetime").date(2026, 5, 1),
            unit_price_base=Decimal("7.00"),
            metro=tenant.metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=line.id,
        )
    )
    db_session.commit()

    client = TestClient(app)
    resp = client.post(f"/review/{line.id}/reopen", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["review_status"] == ReviewStatus.pending.value

    db_session.expire_all()
    assert db_session.get(InvoiceLineItem, line.id).review_status == ReviewStatus.pending
    # The disputed price must stop feeding analytics immediately.
    assert (
        db_session.scalar(
            select(PriceObservation).where(PriceObservation.invoice_line_item_id == line.id)
        )
        is None
    )

    # And it's back in the queue a human actually looks at.
    resp = client.get("/review/queue", params={"tenant_id": str(tenant.id)})
    assert str(line.id) in {item["id"] for item in resp.json()}


def test_reopen_rejects_an_already_pending_line(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.review_status = ReviewStatus.pending
    db_session.commit()

    client = TestClient(app)
    resp = client.post(f"/review/{line.id}/reopen", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 409, resp.text


def test_endpoints_404_on_an_unknown_tenant():
    client = TestClient(app)
    ghost = uuid.uuid4()
    for path, params in [
        ("/invoices", {"tenant_id": str(ghost)}),
        ("/insights", {"tenant_id": str(ghost)}),
        ("/negotiation", {"tenant_id": str(ghost)}),
    ]:
        resp = client.get(path, params=params)
        assert resp.status_code == 404, f"{path} returned {resp.status_code}, expected 404"


# --- worker: what may feed analytics, and retry safety ---------------------


def test_an_invoice_that_fails_arithmetic_writes_no_price_observations(db_session, tenant, monkeypatch):
    """A misread price must not reach benchmarks just because the line's
    identity was certain. The mozzarella line still auto-matches through the
    alias, but its extended price no longer equals qty x unit price, so the
    invoice lands in needs_review — and used to write the observation anyway,
    because observations were written before the arithmetic check ran.
    """
    misread = FAKE_PAYLOAD.model_copy(deep=True)
    misread.line_items[0].unit_price = "74.50"  # 47.50 read as 74.50; extended still 95.00
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(misread))
    _pin_fake_line_to_auto(db_session, tenant)
    invoice_id = _upload(tenant)

    process_invoice(str(invoice_id))

    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.needs_review
    auto_lines = db_session.scalars(
        select(InvoiceLineItem).where(
            InvoiceLineItem.invoice_id == invoice_id, InvoiceLineItem.review_status == ReviewStatus.auto
        )
    ).all()
    assert auto_lines, "the alias should still identify the line — identity isn't what's in doubt"
    assert _observations_for(db_session, invoice_id) == []


def test_confirming_a_line_on_an_unverified_invoice_writes_no_observation(db_session, tenant, distributor, canonical_sku):
    """The review queue's half of the same gap: confirming the SKU is a
    statement about identity, not about whether the price was read right.
    """
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.review_status = ReviewStatus.pending
    db_session.get(Invoice, line.invoice_id).status = InvoiceStatus.needs_review
    db_session.commit()

    resp = TestClient(app).post(f"/review/{line.id}/confirm", params={"tenant_id": str(tenant.id)})

    assert resp.status_code == 200, resp.text
    assert resp.json()["wrote_price_observation"] is False


def test_rerunning_the_job_does_not_duplicate_line_items(db_session, tenant, monkeypatch):
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())
    invoice_id = _upload(tenant)

    process_invoice(str(invoice_id))
    first = len(_line_ids(db_session, invoice_id))
    # The same id enqueued twice: a completed invoice is left alone.
    process_invoice(str(invoice_id))
    assert len(_line_ids(db_session, invoice_id)) == first

    # A retry after a failure: whatever the earlier attempt left is replaced.
    db_session.expire_all()
    db_session.get(Invoice, invoice_id).status = InvoiceStatus.failed
    db_session.commit()
    process_invoice(str(invoice_id))
    assert len(_line_ids(db_session, invoice_id)) == first


def test_an_alert_refresh_failure_does_not_fail_a_committed_invoice(db_session, tenant, monkeypatch):
    """The invoice is durably committed before alerts are refreshed. A failure
    in that derived step used to flip it to `failed` while its observations
    kept feeding analytics.
    """
    def _boom(*_args, **_kwargs):
        raise RuntimeError("transient database error")

    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())
    monkeypatch.setattr("app.workers.tasks.upsert_creep_alerts", _boom)
    _pin_fake_line_to_auto(db_session, tenant)
    invoice_id = _upload(tenant)

    process_invoice(str(invoice_id))  # must not raise

    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.extracted
    assert _observations_for(db_session, invoice_id), "the committed observations stand"


def test_a_failed_extraction_still_records_what_it_cost(db_session, tenant, monkeypatch):
    """Both attempts were billed. Dropping the cost undercounts exactly the
    hard invoices SPEC.md's per-invoice cost gate most needs to see.
    """
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.workers.tasks.extractor",
        _FixedExtractor(error=ExtractionFailedError("did not validate", cost_usd=0.0731)),
    )
    invoice_id = _upload(tenant)

    with pytest.raises(ExtractionFailedError):
        process_invoice(str(invoice_id))

    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.failed
    assert invoice.extraction_cost_usd == Decimal("0.0731")


# --- reopen, and upload validation -----------------------------------------


def test_reopen_removes_the_disputed_alias_too(db_session, tenant, distributor, canonical_sku):
    """Otherwise the tenant's own alias re-applies the disputed mapping at
    confidence 1.0 on the very next invoice.
    """
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    db_session.add(
        SkuAlias(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=canonical_sku.id,
            distributor_id=distributor.id,
            raw_description=line.raw_description,
            raw_sku=line.raw_sku,
        )
    )
    db_session.commit()

    resp = TestClient(app).post(f"/review/{line.id}/reopen", params={"tenant_id": str(tenant.id)})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    remaining = db_session.scalars(
        select(SkuAlias).where(SkuAlias.tenant_id == tenant.id, SkuAlias.raw_sku == line.raw_sku)
    ).all()
    assert remaining == []


def test_upload_rejects_a_file_that_is_not_a_pdf(tenant):
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("invoice.pdf", b"PK\x03\x04 this is a renamed docx", "application/pdf")},
    )
    assert resp.status_code == 415, resp.text


def test_upload_rejects_an_oversized_file_before_writing_it(tenant, monkeypatch):
    monkeypatch.setattr("app.api.invoices.settings.max_upload_bytes", 1024)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("big.pdf", b"%PDF-1.4" + b"0" * 4096, "application/pdf")},
    )
    assert resp.status_code == 413, resp.text


def test_upload_accepts_a_pdf_whose_header_isnt_at_byte_zero(tenant, monkeypatch):
    """Readers accept the header anywhere in the first 1024 bytes, and real
    files arrive with a BOM or a stray newline in front of it."""
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("invoice.pdf", b"\xef\xbb\xbf\r\n" + _make_test_pdf(), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text


def _photo(size=(1200, 1600), fmt="JPEG", orientation=None, mode="RGB") -> bytes:
    from PIL import Image

    image = Image.new(mode, size, {"RGB": "white", "L": 255}.get(mode, (255, 255, 255, 0)))
    buf = io.BytesIO()
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif
    if fmt == "HEIF":
        import pillow_heif

        pillow_heif.from_pillow(image).save(buf, quality=60)
    else:
        image.save(buf, fmt, **kwargs)
    return buf.getvalue()


def _post_files(tenant, files):
    return TestClient(app).post(f"/invoices?tenant_id={tenant.id}", files=[("file", f) for f in files])


def test_photos_of_a_paper_invoice_become_one_invoice_a_page_each(db_session, tenant, monkeypatch):
    """Phones store most photos sideways with an EXIF flag; the page must come
    out upright, letter-sized, and marked as a photo upload."""
    import pypdfium2 as pdfium

    from app.models import AuditEvent
    from app.storage import read_uri

    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    resp = _post_files(
        tenant,
        [
            ("IMG_0001.jpg", _photo((4032, 3024), orientation=6), "image/jpeg"),  # landscape pixels, portrait photo
            ("IMG_0002.png", _photo((1500, 2000), fmt="PNG", mode="RGBA"), "image/png"),
        ],
    )
    assert resp.status_code == 201, resp.text
    invoice = db_session.get(Invoice, uuid.UUID(resp.json()["id"]))
    assert invoice.source == InvoiceSource.photo

    pdf = pdfium.PdfDocument(read_uri(invoice.original_file_uri))
    sizes = [page.get_size() for page in pdf]
    assert len(sizes) == 2
    for width, height in sizes:
        assert height > width, "the rotated photo must come out upright"
        assert abs(height - 11 * 72) < 1, "a letter-sized page, not one inch per 72 pixels"

    event = db_session.scalar(select(AuditEvent).where(AuditEvent.entity_id == invoice.id))
    assert event.details["photo_count"] == 2
    assert event.details["filenames"] == ["IMG_0001.jpg", "IMG_0002.png"]


def test_a_photo_invoice_goes_through_extraction_like_a_pdf(db_session, tenant, monkeypatch):
    from app.storage import page_names

    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())
    resp = _post_files(tenant, [("a.jpg", _photo(), "image/jpeg"), ("b.jpg", _photo(), "image/jpeg")])
    assert resp.status_code == 201, resp.text
    invoice_id = uuid.UUID(resp.json()["id"])
    process_invoice(str(invoice_id))
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status in (InvoiceStatus.extracted, InvoiceStatus.needs_review)
    assert page_names(invoice_id) == ["page_001.png", "page_002.png"]


def test_an_iphone_heic_photo_is_accepted(db_session, tenant, monkeypatch):
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    resp = _post_files(tenant, [("IMG_0003.HEIC", _photo(fmt="HEIF"), "image/heic")])
    assert resp.status_code == 201, resp.text
    assert db_session.get(Invoice, uuid.UUID(resp.json()["id"])).source == InvoiceSource.photo


@pytest.mark.parametrize(
    "files, message",
    [
        ([("a.pdf", _make_test_pdf(), "application/pdf"), ("b.jpg", _photo(), "image/jpeg")], "Upload a PDF on its own"),
        ([("a.pdf", _make_test_pdf(), "application/pdf")] * 2, "Upload a PDF on its own"),
        ([("p.jpg", _photo((200, 200)), "image/jpeg")] * 11, "up to 10 photos"),
        ([("a.jpg", _photo(), "image/jpeg"), ("notes.txt", b"hello", "text/plain")], "File 2 isn't a PDF or a photo."),
        # The right signature, and nothing readable after it.
        ([("broken.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64, "image/jpeg")], "We couldn't open photo 1."),
        # 64 MP: a small file that would decode to ~200 MB in the API process.
        ([("huge.png", _photo((8000, 8000), fmt="PNG", mode="L"), "image/png")], "Photo 1 is too big."),
    ],
)
def test_uploads_that_arent_one_invoice_are_refused_before_anything_is_stored(db_session, tenant, monkeypatch, files, message):
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    resp = _post_files(tenant, files)
    assert resp.status_code == 415, resp.text
    assert message in resp.json()["detail"]
    assert db_session.scalar(select(Invoice.id).where(Invoice.tenant_id == tenant.id)) is None


def test_the_size_limit_covers_all_the_photos_together(db_session, tenant, monkeypatch):
    photo = _photo()
    monkeypatch.setattr("app.api.invoices.settings.max_upload_bytes", len(photo) * 2)
    resp = _post_files(tenant, [("p.jpg", photo, "image/jpeg")] * 3)
    assert resp.status_code == 413, resp.text


def test_the_worker_does_not_match_lines_against_the_other_pseudo_distributor(db_session, tenant, monkeypatch):
    """'other' is extraction's "couldn't tell", not a catalog. Matching against
    it pooled item codes from every distributor that ever landed there."""
    other = db_session.scalar(select(Distributor).where(Distributor.slug == "other"))
    sku = db_session.scalar(select(CanonicalSku).where(CanonicalSku.name == "Mozzarella Shredded Whole Milk"))
    assert other is not None and sku is not None, "seed data missing — run `make seed`"
    # A correction pooled under 'other' from some earlier unattributed invoice.
    db_session.add(SkuAlias(tenant_id=tenant.id, canonical_sku_id=sku.id, distributor_id=other.id,
                            raw_description="MOZZ SHRD WHL MLK 4/5 LB", raw_sku="4001122"))
    db_session.commit()
    unattributed = FAKE_PAYLOAD.model_copy(update={"distributor": "other"})
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(unattributed))
    invoice_id = _upload(tenant)

    process_invoice(str(invoice_id))

    db_session.expire_all()
    lines = db_session.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)).all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.needs_review
    assert lines and all(line.canonical_sku_id is None for line in lines)
    assert all(line.review_status == ReviewStatus.pending for line in lines)


def test_lines_on_an_unattributed_invoice_wait_for_a_distributor(db_session, tenant, canonical_sku):
    """Resolving them here used to write no alias and then be overwritten when
    the distributor was chosen on the invoice screen. They stay out of the
    queue, and acting on one directly is refused, until it has a distributor."""
    other = db_session.scalar(select(Distributor).where(Distributor.slug == "other"))
    line = _auto_matched_line(db_session, tenant, other, canonical_sku)
    line.review_status = ReviewStatus.pending
    db_session.commit()
    client = TestClient(app)

    queue = client.get("/review/queue", params={"tenant_id": str(tenant.id)}).json()
    resp = client.post(
        f"/review/{line.id}/correct",
        params={"tenant_id": str(tenant.id)},
        json={"canonical_sku_id": str(canonical_sku.id)},
    )

    assert str(line.id) not in {item["id"] for item in queue}
    assert resp.status_code == 409, resp.text
    assert "distributor" in resp.json()["detail"]
    assert db_session.scalars(select(SkuAlias).where(SkuAlias.tenant_id == tenant.id)).all() == []


def test_a_running_retry_cant_wipe_lines_typed_into_a_failed_invoice(db_session, tenant, distributor, canonical_sku):
    """The review screen and the worker lock the same invoice row. A person
    who takes a failed invoice under review first wins: the worker, blocked
    on the lock until then, sees needs_review and leaves the lines alone."""
    import threading

    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    invoice_id = line.invoice_id
    db_session.get(Invoice, invoice_id).status = InvoiceStatus.failed
    db_session.commit()

    reviewer = SessionLocal()
    bind_tenant(reviewer, tenant.id)
    worker = threading.Thread(target=process_invoice, args=(str(invoice_id),))
    # try/finally: if the worker doesn't wait (the regression this test is
    # for), the assertion must fail the test, not leave this session holding
    # the row lock so the fixture's teardown blocks on it forever.
    try:
        held = reviewer.get(Invoice, invoice_id, with_for_update=True)  # the edit, in progress
        worker.start()
        worker.join(timeout=1.0)
        waited = worker.is_alive()
        held.status = InvoiceStatus.needs_review  # what _take_under_review does
        reviewer.commit()
    finally:
        reviewer.close()
        worker.join(timeout=10)
    assert waited, "the worker must wait for the reviewer's lock, not race past it"

    db_session.expire_all()
    assert db_session.get(InvoiceLineItem, line.id) is not None
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.needs_review


def test_reopen_withdraws_the_disputed_mapping_for_the_whole_business(db_session, tenant, distributor, canonical_sku):
    """A sibling location's copy of the same wrong mapping is trusted by the
    matcher as this location's own (account-keyed), so reopen has to withdraw
    it too. Another business's correction is theirs and stays."""
    group = Account(name=f"Reopen Group {uuid.uuid4().hex[:8]}")
    db_session.add(group)
    db_session.commit()
    sibling = Tenant(name=f"Sibling {uuid.uuid4().hex[:8]}", metro=tenant.metro, volume_tier=VolumeTier.under_500k, account_id=group.id)
    stranger = Tenant(name=f"Stranger {uuid.uuid4().hex[:8]}", metro=tenant.metro, volume_tier=VolumeTier.under_500k)
    db_session.add_all([sibling, stranger])
    db_session.get(Tenant, tenant.id).account_id = group.id
    db_session.commit()
    db_session.info["_created"]["tenants"] += [sibling.id, stranger.id]

    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)

    def alias_for(owner):
        return SkuAlias(tenant_id=owner.id, canonical_sku_id=canonical_sku.id, distributor_id=distributor.id,
                        raw_description=line.raw_description, raw_sku=line.raw_sku)
    siblings_copy, strangers_copy = alias_for(sibling), alias_for(stranger)
    db_session.add_all([siblings_copy, strangers_copy])
    db_session.commit()
    sibling_id, stranger_id = siblings_copy.id, strangers_copy.id

    resp = TestClient(app).post(f"/review/{line.id}/reopen", params={"tenant_id": str(tenant.id)})

    assert resp.status_code == 200, resp.text
    db_session.expire_all()
    assert db_session.get(SkuAlias, sibling_id) is None
    assert db_session.get(SkuAlias, stranger_id) is not None
    db_session.get(Tenant, tenant.id).account_id = None  # let teardown remove the account
    db_session.get(Tenant, sibling.id).account_id = None
    db_session.commit()
    db_session.delete(db_session.get(Account, group.id))
    db_session.commit()


def test_a_queue_action_waits_for_an_invoice_being_re_attributed(db_session, tenant, distributor, canonical_sku):
    """Confirm and correct take the same invoice lock the invoice screen and
    the worker take, so they can't interleave with a distributor change."""
    import threading

    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.review_status = ReviewStatus.pending
    db_session.commit()

    screen = SessionLocal()
    bind_tenant(screen, tenant.id)
    result = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "status", TestClient(app).post(f"/review/{line.id}/confirm", params={"tenant_id": str(tenant.id)}).status_code
        )
    )
    # try/finally for the same reason as the worker-lock test above: a
    # regression must fail here, not hang teardown on a leaked row lock.
    try:
        screen.get(Invoice, line.invoice_id, with_for_update=True)  # a re-attribution in progress
        worker.start()
        worker.join(timeout=1.0)
        waited = worker.is_alive()
        # The re-attribution re-matched the line (here: resolved it) before letting go.
        screen.get(InvoiceLineItem, line.id).review_status = ReviewStatus.auto
        screen.commit()
    finally:
        screen.close()
        worker.join(timeout=10)
    assert waited, "the confirm must wait for the invoice lock"

    # Re-read under the lock, the line is no longer pending: nothing written.
    assert result["status"] == 409
    assert db_session.scalars(select(SkuAlias).where(SkuAlias.tenant_id == tenant.id)).all() == []


@pytest.mark.parametrize("printed, expected_date, expected_status", [
    ("07/07/2026", __import__("datetime").date(2026, 7, 7), None),  # as printed, month first
    ("", None, InvoiceStatus.needs_review),  # none printed: held for a person, not failed
])
def test_the_worker_reads_printed_dates_and_holds_invoices_without_one(
    db_session, tenant, monkeypatch, printed, expected_date, expected_status
):
    payload = FAKE_PAYLOAD.model_copy(update={"invoice_date": printed, "delivery_date": None})
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(payload))
    invoice_id = _upload(tenant)
    process_invoice(str(invoice_id))
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.invoice_date == expected_date
    assert invoice.status != InvoiceStatus.failed
    if expected_status:
        assert invoice.status == expected_status


def test_a_product_priced_in_a_unit_the_pack_cant_convert_to_is_refused(db_session, tenant, distributor):
    """Picking Canola Oil (per gallon) for a 35 lb jug used to record the price
    per pound as a price per gallon, in that product's history and benchmarks."""
    per_gallon = CanonicalSku(name=f"Review API Oil {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.gal)
    db_session.add(per_gallon)
    db_session.commit()
    db_session.info["_created"]["skus"].append(per_gallon.id)
    line = _auto_matched_line(db_session, tenant, distributor, per_gallon)
    line.raw_pack_size, line.uom, line.review_status = "35 LB", "CS", ReviewStatus.pending
    line.canonical_sku_id = line.normalized_unit_price = line.normalized_qty_base = None
    db_session.commit()

    resp = TestClient(app).post(
        f"/review/{line.id}/correct", params={"tenant_id": str(tenant.id)}, json={"canonical_sku_id": str(per_gallon.id)}
    )

    assert resp.status_code == 400, resp.text
    assert "priced per gallon" in resp.json()["detail"] and "pounds" in resp.json()["detail"]
    db_session.expire_all()
    assert db_session.get(InvoiceLineItem, line.id).canonical_sku_id is None
    assert db_session.scalars(select(PriceObservation).where(PriceObservation.tenant_id == tenant.id)).all() == []


def test_a_product_in_a_convertible_unit_is_priced_in_its_own(db_session, tenant, distributor):
    """"12 DZ" bar towels matched to a product sold each: $28.80 a case is
    $0.20 a towel, and that's what goes into its price history."""
    each = CanonicalSku(name=f"Review API Towel {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.each)
    db_session.add(each)
    db_session.commit()
    db_session.info["_created"]["skus"].append(each.id)
    line = _auto_matched_line(db_session, tenant, distributor, each)
    line.raw_pack_size, line.uom, line.unit_price, line.extended_price = "12 DZ", "CS", Decimal("28.80"), Decimal("28.80")
    line.review_status = ReviewStatus.pending
    line.canonical_sku_id = line.normalized_unit_price = line.normalized_qty_base = None
    db_session.commit()

    resp = TestClient(app).post(
        f"/review/{line.id}/correct", params={"tenant_id": str(tenant.id)}, json={"canonical_sku_id": str(each.id)}
    )

    assert resp.status_code == 200, resp.text
    observation = db_session.scalar(select(PriceObservation).where(PriceObservation.invoice_line_item_id == line.id))
    assert observation.unit_price_base == Decimal("0.2000")


def test_the_queue_says_when_an_items_price_cant_be_worked_out(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.raw_pack_size, line.uom, line.review_status = "1000 CT", "EA", ReviewStatus.pending
    line.normalized_unit_price = line.normalized_qty_base = None
    db_session.commit()

    queue = TestClient(app).get("/review/queue", params={"tenant_id": str(tenant.id)}).json()

    item = next(i for i in queue if i["id"] == str(line.id))
    assert item["price_known"] is False


def test_a_liquid_with_a_weight_per_gallon_takes_a_pack_sold_by_weight(db_session, tenant, distributor):
    """Fry oil comes in 35 lb jugs; Canola Oil is priced per gallon. With its
    7.7 lb/gal, $40.67 a jug is $8.95 a gallon."""
    oil = CanonicalSku(
        name=f"Review API Fry Oil {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.gal, lb_per_gal=Decimal("7.7")
    )
    db_session.add(oil)
    db_session.commit()
    db_session.info["_created"]["skus"].append(oil.id)
    line = _auto_matched_line(db_session, tenant, distributor, oil)
    line.raw_pack_size, line.uom, line.unit_price, line.extended_price = "35 LB", "CS", Decimal("40.67"), Decimal("40.67")
    line.review_status = ReviewStatus.pending
    line.canonical_sku_id = line.normalized_unit_price = line.normalized_qty_base = None
    db_session.commit()

    resp = TestClient(app).post(
        f"/review/{line.id}/correct", params={"tenant_id": str(tenant.id)}, json={"canonical_sku_id": str(oil.id)}
    )

    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["normalized_unit_price"]) == Decimal("8.9474")
