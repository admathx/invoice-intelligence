"""Grouping locations into a business, and what that grouping then changes.

The last two tests are the point of the whole feature: an account isn't
bookkeeping, it's the thing that stops a multi-unit operator from clearing its
own benchmark suppression threshold and from corroborating its own SKU
corrections. Both behaviours shipped before there was any way to create an
account, so neither had ever been reachable end to end.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.analytics.benchmark import MIN_DISTINCT_ACCOUNTS, compute_benchmark
from app.db import SessionLocal
from app.main import app
from app.models import Account, CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    """A committing session, because the endpoints under test open their own
    (via get_db) and would not see uncommitted fixture rows. Everything it
    creates is torn down explicitly, in FK order.
    """
    session = SessionLocal()
    created: dict[str, list] = {"tenants": [], "accounts": [], "distributors": [], "skus": []}
    session.info["_created"] = created
    try:
        yield session
    finally:
        session.rollback()
        if created["tenants"]:
            lines = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id.in_(created["tenants"]))
            session.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(lines)))
            session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(created["tenants"])))
            session.execute(delete(Invoice).where(Invoice.tenant_id.in_(created["tenants"])))
            session.execute(delete(Tenant).where(Tenant.id.in_(created["tenants"])))
        if created["accounts"]:
            session.execute(delete(Account).where(Account.id.in_(created["accounts"])))
        if created["distributors"]:
            session.execute(delete(Distributor).where(Distributor.id.in_(created["distributors"])))
        if created["skus"]:
            session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(created["skus"])))
        session.commit()
        session.close()


def _make_tenant(db, metro: str = "accounts-test-metro") -> Tenant:
    tenant = Tenant(name=f"Accounts Test Tenant {uuid.uuid4().hex[:8]}", metro=metro, volume_tier=VolumeTier.under_500k)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    db.info["_created"]["tenants"].append(tenant.id)
    return tenant


def _track_account(db, account_id: str) -> uuid.UUID:
    db.info["_created"]["accounts"].append(uuid.UUID(account_id))
    return uuid.UUID(account_id)


# --- the API ----------------------------------------------------------------


def test_create_and_read_back_an_account(client, db):
    created = client.post("/accounts", json={"name": "Harbor Group"})
    assert created.status_code == 201, created.text
    account_id = _track_account(db, created.json()["id"])

    assert created.json()["location_count"] == 0
    fetched = client.get(f"/accounts/{account_id}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Harbor Group"


def test_an_account_rejects_an_empty_name(client):
    assert client.post("/accounts", json={"name": ""}).status_code == 422


def test_attaching_locations_reports_them_back(client, db):
    account_id = _track_account(db, client.post("/accounts", json={"name": "Two Location Group"}).json()["id"])
    first, second = _make_tenant(db), _make_tenant(db)

    for tenant in (first, second):
        resp = client.post(f"/accounts/{account_id}/locations", json={"tenant_id": str(tenant.id)})
        assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["location_count"] == 2
    assert {loc["id"] for loc in body["locations"]} == {str(first.id), str(second.id)}
    assert client.get("/tenants?unassigned=true").status_code == 200
    unassigned = {t["id"] for t in client.get("/tenants?unassigned=true").json()}
    assert str(first.id) not in unassigned and str(second.id) not in unassigned


def test_moving_a_location_between_businesses_is_rejected_not_silent(client, db):
    """Re-parenting changes which cells a location can corroborate and whose
    corrections it inherits — too consequential to happen as a side effect.
    """
    first = _track_account(db, client.post("/accounts", json={"name": "First Group"}).json()["id"])
    second = _track_account(db, client.post("/accounts", json={"name": "Second Group"}).json()["id"])
    tenant = _make_tenant(db)
    client.post(f"/accounts/{first}/locations", json={"tenant_id": str(tenant.id)})

    conflict = client.post(f"/accounts/{second}/locations", json={"tenant_id": str(tenant.id)})

    assert conflict.status_code == 409
    assert "detach it first" in conflict.json()["detail"]


def test_detaching_returns_the_location_to_the_unassigned_pool(client, db):
    account_id = _track_account(db, client.post("/accounts", json={"name": "Shrinking Group"}).json()["id"])
    tenant = _make_tenant(db)
    client.post(f"/accounts/{account_id}/locations", json={"tenant_id": str(tenant.id)})

    resp = client.delete(f"/accounts/{account_id}/locations/{tenant.id}")

    assert resp.status_code == 200
    assert resp.json()["location_count"] == 0
    assert str(tenant.id) in {t["id"] for t in client.get("/tenants?unassigned=true").json()}


def test_unknown_ids_404_rather_than_500(client, db):
    account_id = _track_account(db, client.post("/accounts", json={"name": "Lonely Group"}).json()["id"])
    missing = uuid.uuid4()

    assert client.get(f"/accounts/{missing}").status_code == 404
    assert client.post(f"/accounts/{missing}/locations", json={"tenant_id": str(missing)}).status_code == 404
    assert client.post(f"/accounts/{account_id}/locations", json={"tenant_id": str(missing)}).status_code == 404
    assert client.delete(f"/accounts/{account_id}/locations/{missing}").status_code == 404


# --- what grouping actually changes ----------------------------------------


def _add_observation(db, tenant, sku, distributor, price: str) -> None:
    observed_on = AS_OF - timedelta(days=10)
    invoice_id, line_id = uuid.uuid4(), uuid.uuid4()
    db.add(
        Invoice(
            id=invoice_id, tenant_id=tenant.id, distributor_id=distributor.id, invoice_date=observed_on,
            source=InvoiceSource.upload, original_file_uri="file:///dev/null", status=InvoiceStatus.extracted,
        )
    )
    db.add(
        InvoiceLineItem(
            id=line_id, tenant_id=tenant.id, invoice_id=invoice_id, line_number=1,
            raw_description="Accounts Test Line", quantity=Decimal("1"), unit_price=Decimal(price),
            extended_price=Decimal(price), uom="LB", canonical_sku_id=sku.id,
            normalized_qty_base=Decimal("1"), normalized_unit_price=Decimal(price),
            base_uom=sku.base_uom, review_status=ReviewStatus.auto,
        )
    )
    db.add(
        PriceObservation(
            id=uuid.uuid4(), tenant_id=tenant.id, canonical_sku_id=sku.id, distributor_id=distributor.id,
            observed_on=observed_on, unit_price_base=Decimal(price), metro=tenant.metro,
            volume_tier=tenant.volume_tier, invoice_line_item_id=line_id,
        )
    )
    db.commit()


def test_grouping_locations_suppresses_a_cell_they_were_clearing_alone(client, db):
    """The feature's whole reason to exist, now reachable through the API: five
    locations of one business clear a five-business threshold until somebody
    says they are one business.
    """
    metro = f"accounts-metro-{uuid.uuid4().hex[:8]}"
    distributor = Distributor(name="Accounts Test Distributor", slug=f"accounts-{uuid.uuid4().hex[:8]}")
    db.add(distributor)
    sku = CanonicalSku(name=f"Accounts Test SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db.add(sku)
    db.commit()
    db.info["_created"]["distributors"].append(distributor.id)
    db.info["_created"]["skus"].append(sku.id)

    locations = [_make_tenant(db, metro) for _ in range(MIN_DISTINCT_ACCOUNTS)]
    for tenant in locations:
        _add_observation(db, tenant, sku, distributor, "9.00")

    assert compute_benchmark(db, sku.id, metro, AS_OF) is not None, "five separate tenants should clear it"

    account_id = _track_account(db, client.post("/accounts", json={"name": "Five Location Group"}).json()["id"])
    for tenant in locations:
        assert client.post(f"/accounts/{account_id}/locations", json={"tenant_id": str(tenant.id)}).status_code == 200

    db.expire_all()  # the endpoints committed through their own session
    assert compute_benchmark(db, sku.id, metro, AS_OF) is None, "one business must not clear its own threshold"
