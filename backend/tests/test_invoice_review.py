"""The invoice review screen's API: fixing the numbers on an invoice that failed
the arithmetic check, and confirming it back into analytics.

Before this, an invoice in `needs_review` was a dead end. Its prices were
(rightly) kept out of analytics, but nothing could ever let them back in, so
one misread digit silently removed a real invoice from every benchmark.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, bind_tenant
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


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    """Committing session: the endpoints open their own sessions and wouldn't
    see uncommitted fixture rows. Everything created is removed in FK order."""
    session = SessionLocal()
    created: dict[str, list] = {"tenants": [], "distributors": [], "skus": []}
    session.info["_created"] = created
    try:
        yield session
    finally:
        session.rollback()
        tenants = created["tenants"]
        if tenants:
            lines = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id.in_(tenants))
            session.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(lines)))
            session.execute(delete(PriceAlert).where(PriceAlert.tenant_id.in_(tenants)))
            session.execute(delete(SkuAlias).where(SkuAlias.tenant_id.in_(tenants)))
            session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(tenants)))
            session.execute(delete(Invoice).where(Invoice.tenant_id.in_(tenants)))
            session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        if created["skus"]:
            session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(created["skus"])))
        if created["distributors"]:
            session.execute(delete(Distributor).where(Distributor.id.in_(created["distributors"])))
        session.commit()
        session.close()


@pytest.fixture()
def tenant(db):
    t = Tenant(name=f"Invoice Review Tenant {uuid.uuid4().hex[:8]}", metro="invoice-review-metro", volume_tier=VolumeTier.under_500k)
    db.add(t)
    db.commit()
    db.refresh(t)
    db.info["_created"]["tenants"].append(t.id)
    bind_tenant(db, t.id)
    return t


@pytest.fixture()
def distributor(db):
    d = Distributor(name="Invoice Review Distributor", slug=f"invoice-review-{uuid.uuid4().hex[:8]}")
    db.add(d)
    db.commit()
    db.refresh(d)
    db.info["_created"]["distributors"].append(d.id)
    return d


def _sku(db, name: str) -> CanonicalSku:
    sku = CanonicalSku(name=f"{name} {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db.add(sku)
    db.commit()
    db.refresh(sku)
    db.info["_created"]["skus"].append(sku.id)
    return sku


def _line(invoice, tenant, number, qty, unit, ext, pack, sku, status, normalized_price=None) -> InvoiceLineItem:
    return InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        line_number=number,
        raw_description=f"REVIEW TEST LINE {number}",
        raw_sku=f"RT-{number}",
        raw_pack_size=pack,
        quantity=Decimal(qty),
        unit_price=Decimal(unit),
        extended_price=Decimal(ext),
        uom="CS",
        canonical_sku_id=sku.id if sku else None,
        normalized_qty_base=Decimal(qty) * 20 if sku else None,
        normalized_unit_price=Decimal(normalized_price) if normalized_price else None,
        base_uom=BaseUom.lb if sku else None,
        review_status=status,
    )


@pytest.fixture()
def misread_invoice(db, tenant, distributor):
    """Line 1's unit price was read as $74.50 when the page says $47.50, so
    2 x 74.50 != 95.00 and the worker sent the invoice to needs_review."""
    mozzarella, tomato = _sku(db, "Mozzarella"), _sku(db, "Tomato")
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id,
        invoice_number="REVIEW-0001",
        invoice_date=date(2026, 5, 1),
        subtotal=Decimal("152.50"),
        tax=Decimal("0.00"),
        total=Decimal("152.50"),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.needs_review,
    )
    db.add(invoice)
    lines = [
        _line(invoice, tenant, 1, "2", "74.50", "95.00", "4/5 LB", mozzarella, ReviewStatus.auto, "3.7250"),
        _line(invoice, tenant, 2, "1", "47.50", "47.50", "4/5 LB", tomato, ReviewStatus.auto, "2.3750"),
        _line(invoice, tenant, 3, "1", "10.00", "10.00", "4/5 LB", None, ReviewStatus.pending),
    ]
    db.add_all(lines)
    db.commit()
    return invoice, lines


def _url(invoice, tenant, suffix=""):
    return f"/invoices/{invoice.id}{suffix}?tenant_id={tenant.id}"


def _observations(db, lines) -> dict[uuid.UUID, PriceObservation]:
    db.expire_all()
    rows = db.scalars(select(PriceObservation).where(PriceObservation.invoice_line_item_id.in_([l.id for l in lines])))
    return {row.invoice_line_item_id: row for row in rows}


# --- the check the screen shows ---------------------------------------------


def test_the_detail_explains_what_doesnt_add_up(client, misread_invoice, tenant):
    invoice, _ = misread_invoice

    body = client.get(_url(invoice, tenant)).json()

    assert body["check"]["passes"] is False
    assert body["check"]["failed_line_numbers"] == [1]
    assert any("line(s) [1]" in reason for reason in body["check"]["reasons"])


def test_confirming_is_refused_while_the_numbers_still_dont_add_up(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.post(_url(invoice, tenant, "/confirm"))

    assert resp.status_code == 422
    assert resp.json()["detail"]["reasons"]
    assert _observations(db, lines) == {}


# --- fixing and confirming --------------------------------------------------


def test_correcting_the_misread_price_reprices_the_line_and_passes_the_check(client, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["check"]["passes"] is True
    fixed = next(li for li in body["line_items"] if li["line_number"] == 1)
    # Re-derived from the corrected number, not left at the misread one:
    # $47.50 per 20 lb case is $2.3750/lb (it was $3.7250 from $74.50).
    assert Decimal(fixed["normalized_unit_price"]) == Decimal("2.3750")
    # Editing doesn't confirm: the person decides when they're done.
    assert body["status"] == InvoiceStatus.needs_review.value


def test_confirming_lets_the_invoice_back_into_analytics(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    resp = client.post(_url(invoice, tenant, "/confirm"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == InvoiceStatus.confirmed.value
    observations = _observations(db, lines)
    # Both settled lines, at the corrected price...
    assert set(observations) == {lines[0].id, lines[1].id}
    assert observations[lines[0].id].unit_price_base == Decimal("2.3750")
    # ...but not the line whose SKU is still unknown.
    assert lines[2].id not in observations


def test_a_line_resolved_later_on_a_confirmed_invoice_still_reaches_analytics(client, db, misread_invoice, tenant):
    """The pending line waits for the line queue; when that resolves it, the
    invoice is `confirmed` rather than `extracted`, and must still count."""
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})
    client.post(_url(invoice, tenant, "/confirm"))

    resp = client.post(
        f"/review/{lines[2].id}/correct",
        params={"tenant_id": str(tenant.id)},
        json={"canonical_sku_id": str(lines[1].canonical_sku_id)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["wrote_price_observation"] is True
    assert lines[2].id in _observations(db, lines)


def test_an_invoice_can_only_be_confirmed_once(client, misread_invoice, tenant):
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})
    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 200

    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 409


# --- what can't be edited ---------------------------------------------------


def test_an_extracted_invoice_cant_be_edited(client, db, misread_invoice, tenant):
    """Its numbers already feed analytics; rewriting them in place would
    silently move benchmarks other tenants are reading."""
    invoice, lines = misread_invoice
    db.get(Invoice, invoice.id).status = InvoiceStatus.extracted
    db.commit()

    resp = client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "1.00"}]})

    assert resp.status_code == 409


def test_a_line_from_another_invoice_is_rejected_and_nothing_changes(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(
        _url(invoice, tenant),
        json={
            "subtotal": "1.00",
            "line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}, {"id": str(uuid.uuid4()), "unit_price": "1"}],
        },
    )

    assert resp.status_code == 422
    db.expire_all()
    assert db.get(Invoice, invoice.id).subtotal == Decimal("152.50")
    assert db.get(InvoiceLineItem, lines[0].id).unit_price == Decimal("74.50")


def test_a_value_too_large_for_the_column_is_a_422_not_a_database_error(client, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(
        _url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "123456789012.5"}]}
    )

    assert resp.status_code == 422


def test_an_unrecognized_distributor_blocks_confirmation_until_one_is_chosen(client, db, misread_invoice, tenant, distributor):
    invoice, lines = misread_invoice
    db.get(Invoice, invoice.id).distributor_id = None
    db.commit()

    reasons = client.get(_url(invoice, tenant)).json()["check"]["reasons"]
    assert any("distributor" in reason for reason in reasons)

    resp = client.patch(
        _url(invoice, tenant),
        json={"distributor_id": str(distributor.id), "line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["distributor_name"] == distributor.name
    assert not any("distributor" in reason for reason in resp.json()["check"]["reasons"])


def test_extractions_other_counts_as_unrecognized_not_as_a_distributor(client, db, misread_invoice, tenant):
    """The worker stores extraction's 'other' as a real distributor row, so a
    NULL check alone let an invoice nobody could attribute be confirmed."""
    invoice, lines = misread_invoice
    other = db.scalar(select(Distributor).where(Distributor.slug == "other"))
    assert other is not None, "distributors not seeded — run `make seed`"
    db.get(Invoice, invoice.id).distributor_id = other.id
    db.commit()
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    check = client.get(_url(invoice, tenant)).json()["check"]
    assert any("distributor" in reason for reason in check["reasons"])
    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 422
    # And it isn't offered as a choice.
    assert "other" not in {d["slug"] for d in client.get("/distributors").json()}


def test_other_cant_be_chosen_as_the_correction(client, db, misread_invoice, tenant):
    invoice, _ = misread_invoice
    other = db.scalar(select(Distributor).where(Distributor.slug == "other"))

    assert client.patch(_url(invoice, tenant), json={"distributor_id": str(other.id)}).status_code == 422
