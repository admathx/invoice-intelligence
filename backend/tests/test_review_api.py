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
from app.extract.client import FakeExtractorClient
from app.main import app
from app.models import (
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


def test_worker_writes_price_observations_for_auto_matched_lines(db_session, tenant, monkeypatch):
    """The whole analytics stack reads exclusively from price_observations —
    if the live pipeline doesn't write them, real invoices never show up in
    price creep, benchmarks, or negotiation sheets.
    """
    monkeypatch.setattr("app.api.invoices.queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr("app.workers.tasks.extractor", FakeExtractorClient())

    # Pin one line to the auto tier deterministically: a confirmed alias makes
    # match_line_item short-circuit with confidence 1.0 / review_status=auto,
    # rather than depending on where the embedding matcher happens to score
    # the fake extractor's canned descriptions.
    sysco = db_session.scalar(select(Distributor).where(Distributor.slug == "sysco"))
    sku = db_session.scalar(select(CanonicalSku).where(CanonicalSku.name == "Mozzarella Shredded Whole Milk"))
    assert sysco is not None and sku is not None, "catalog/distributors not seeded — run `make seed`"
    alias = SkuAlias(
        id=uuid.uuid4(),
        # Owned by this tenant rather than left NULL: a NULL alias is the
        # system-curated tier, which is trusted everywhere without a second
        # confirmation. Attributing it keeps the pin explicit and exercises
        # the path a real correction takes (matcher.match_by_alias rule 1).
        tenant_id=tenant.id,
        canonical_sku_id=sku.id,
        distributor_id=sysco.id,
        raw_description="MOZZ SHRD WHL MLK 4/5 LB",
        raw_sku="4001122",
        pack_size="4/5 LB",
    )
    db_session.add(alias)
    db_session.commit()
    db_session.info["_created"]["aliases"].append(alias.id)

    client = TestClient(app)
    resp = client.post(
        f"/invoices?tenant_id={tenant.id}",
        files={"file": ("test.pdf", _make_test_pdf(), "application/pdf")},
    )
    assert resp.status_code == 201, resp.text
    invoice_id = uuid.UUID(resp.json()["id"])

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
