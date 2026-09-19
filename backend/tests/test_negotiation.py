"""Phase 4c gate, per SPEC.md §7: negotiation sheet ranked by dollars
recoverable, not percentage gap — "a 3% gap on a high-volume SKU above a 40%
gap on a low-volume one."
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.analytics.benchmark import MIN_DISTINCT_TENANTS
from app.analytics.negotiation import build_negotiation_sheet
from app.db import SessionLocal
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def db_session():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Negotiation Test Distributor", slug=f"negotiation-test-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


@pytest.fixture()
def home_tenant(db_session):
    tenant = Tenant(
        name=f"Negotiation Test Tenant {uuid.uuid4().hex[:8]}", metro="negotiation-test-metro", volume_tier=VolumeTier.under_500k
    )
    db_session.add(tenant)
    db_session.commit()
    db_session.refresh(tenant)
    return tenant


def _make_sku(db, name: str) -> CanonicalSku:
    sku = CanonicalSku(name=f"{name} {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db.add(sku)
    db.commit()
    db.refresh(sku)
    return sku


def _add_observation(db, tenant, sku, distributor, metro: str, price: str, qty: str = "10", observed_on: date = AS_OF) -> None:
    invoice_id = uuid.uuid4()
    db.add(
        Invoice(
            id=invoice_id,
            tenant_id=tenant.id,
            distributor_id=distributor.id,
            invoice_date=observed_on,
            source=InvoiceSource.upload,
            original_file_uri="file:///dev/null",
            status=InvoiceStatus.extracted,
        )
    )
    line_item_id = uuid.uuid4()
    db.add(
        InvoiceLineItem(
            id=line_item_id,
            tenant_id=tenant.id,
            invoice_id=invoice_id,
            line_number=1,
            raw_description="Negotiation Test Line",
            quantity=Decimal(qty),
            unit_price=Decimal(price),
            extended_price=Decimal(price) * Decimal(qty),
            uom="LB",
            canonical_sku_id=sku.id,
            normalized_qty_base=Decimal(qty),
            normalized_unit_price=Decimal(price),
            base_uom=sku.base_uom,
            review_status=ReviewStatus.auto,
        )
    )
    db.add(
        PriceObservation(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            observed_on=observed_on,
            unit_price_base=Decimal(price),
            metro=metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=line_item_id,
        )
    )
    db.commit()


def _seed_peer_cell(db, sku, distributor, metro: str, peer_p25_price: str) -> None:
    """MIN_DISTINCT_TENANTS peer tenants all priced at peer_p25_price, so the
    cell's p25 (and every percentile) is exactly that number — a clean,
    predictable benchmark for the test to assert against.
    """
    for _ in range(MIN_DISTINCT_TENANTS):
        peer = Tenant(name=f"Peer Tenant {uuid.uuid4().hex[:8]}", metro=metro, volume_tier=VolumeTier.under_500k)
        db.add(peer)
        db.commit()
        db.refresh(peer)
        _add_observation(db, peer, sku, distributor, metro, peer_p25_price, qty="1")


def test_ranks_dollar_gap_above_percentage_gap(db_session, distributor, home_tenant):
    """A 3% gap on a high-volume SKU (cheese) must outrank a 40% gap on a
    low-volume one (toothpicks) — SPEC.md §7's own worked example.
    """
    cheese = _make_sku(db_session, "Cheese")
    toothpicks = _make_sku(db_session, "Toothpicks")

    _seed_peer_cell(db_session, cheese, distributor, home_tenant.metro, "10.00")
    _seed_peer_cell(db_session, toothpicks, distributor, home_tenant.metro, "1.00")

    # Cheese: tenant pays $10.30 (3% over peer p25 $10.00), buys 1000 lb/quarter.
    _add_observation(db_session, home_tenant, cheese, distributor, home_tenant.metro, "10.30", qty="1000")
    # Toothpicks: tenant pays $1.40 (40% over peer p25 $1.00), buys 5 units/quarter.
    _add_observation(db_session, home_tenant, toothpicks, distributor, home_tenant.metro, "1.40", qty="5")

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    sheet_by_sku = {line.canonical_sku_id: line for line in sheet}

    assert cheese.id in sheet_by_sku and toothpicks.id in sheet_by_sku
    cheese_line = sheet_by_sku[cheese.id]
    toothpicks_line = sheet_by_sku[toothpicks.id]

    assert cheese_line.recoverable_90d > toothpicks_line.recoverable_90d
    ranked_ids = [line.canonical_sku_id for line in sheet]
    assert ranked_ids.index(cheese.id) < ranked_ids.index(toothpicks.id)


def test_skus_priced_at_or_below_peer_p25_are_excluded(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Fairly Priced Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "4.50", qty="100")

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    assert sku.id not in {line.canonical_sku_id for line in sheet}


def test_every_line_traces_to_invoice_line_item_ids(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Traceable Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "8.00", qty="50")

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    line = next(l for l in sheet if l.canonical_sku_id == sku.id)

    assert line.current_price_line_item_id is not None
    assert len(line.quantity_line_item_ids) >= 1
    assert line.current_price_line_item_id in line.quantity_line_item_ids


def test_sheet_capped_at_top_15(db_session, distributor, home_tenant):
    for i in range(20):
        sku = _make_sku(db_session, f"Overpriced Item {i}")
        _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "1.00")
        _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "2.00", qty=str(10 + i))

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    assert len(sheet) == 15
