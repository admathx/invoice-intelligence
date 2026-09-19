"""Phase 4b gate, per SPEC.md §7: "Suppress the cell entirely if it has fewer
than 5 distinct tenants — this is a hard rule, not a tunable." Written before
any benchmark computation touches real corpus data — a suppression bug here
is a privacy leak (an identifiable competitor's price), not just a wrong
number.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.analytics.benchmark import MIN_DISTINCT_TENANTS, compute_benchmark
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
def canonical_sku(db_session):
    sku = CanonicalSku(name=f"Suppression Test SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Suppression Test Distributor", slug=f"suppression-test-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


def _make_tenant(db, metro: str, volume_tier: VolumeTier = VolumeTier.under_500k) -> Tenant:
    tenant = Tenant(name=f"Suppression Test Tenant {uuid.uuid4().hex[:8]}", metro=metro, volume_tier=volume_tier)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_line_item(db, tenant, sku, distributor, observed_on: date, price: str) -> uuid.UUID:
    """A price_observation's invoice_line_item_id is a real FK — build the
    minimal Invoice/InvoiceLineItem it points to.
    """
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
            raw_description="Suppression Test Line",
            quantity=Decimal("1"),
            unit_price=Decimal(price),
            extended_price=Decimal(price),
            uom="LB",
            canonical_sku_id=sku.id,
            normalized_qty_base=Decimal("1"),
            normalized_unit_price=Decimal(price),
            base_uom=sku.base_uom,
            review_status=ReviewStatus.auto,
        )
    )
    return line_item_id


def _add_observation(db, tenant, sku, distributor, metro: str, price: str = "3.00") -> None:
    observed_on = AS_OF - timedelta(days=10)
    line_item_id = _make_line_item(db, tenant, sku, distributor, observed_on, price)
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


def test_four_distinct_tenants_is_suppressed(db_session, canonical_sku, distributor):
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    for _ in range(MIN_DISTINCT_TENANTS - 1):
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is None


def test_five_distinct_tenants_returns_a_result(db_session, canonical_sku, distributor):
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    for _ in range(MIN_DISTINCT_TENANTS):
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is not None
    assert result.scope == "metro"
    assert result.distinct_tenant_count == MIN_DISTINCT_TENANTS


def test_repeated_observations_from_one_tenant_never_clear_suppression(db_session, canonical_sku, distributor):
    """No API response can ever contain a single-tenant-derived number: many
    observations from ONE tenant must not be mistaken for tenant diversity.
    """
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    tenant = _make_tenant(db_session, metro)
    for i in range(20):
        observed_on = AS_OF - timedelta(days=i)
        line_item_id = _make_line_item(db_session, tenant, canonical_sku, distributor, observed_on, "3.00")
        db_session.add(
            PriceObservation(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                canonical_sku_id=canonical_sku.id,
                distributor_id=distributor.id,
                observed_on=observed_on,
                unit_price_base=Decimal("3.00"),
                metro=metro,
                volume_tier=tenant.volume_tier,
                invoice_line_item_id=line_item_id,
            )
        )
    db_session.commit()

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is None


def test_metro_falls_back_to_national(db_session, canonical_sku, distributor):
    """Metro cell alone is under the threshold, but enough tenants exist
    nationally (across other metros) to clear it — falls back, doesn't
    suppress.
    """
    home_metro = f"metro-{uuid.uuid4().hex[:8]}"
    other_metro = f"metro-{uuid.uuid4().hex[:8]}"

    for _ in range(2):
        tenant = _make_tenant(db_session, home_metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, home_metro)
    for _ in range(3):
        tenant = _make_tenant(db_session, other_metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, other_metro)

    result = compute_benchmark(db_session, canonical_sku.id, home_metro, AS_OF)
    assert result is not None
    assert result.scope == "national"
    assert result.distinct_tenant_count == 5


def test_national_also_suppressed_returns_none(db_session, canonical_sku, distributor):
    home_metro = f"metro-{uuid.uuid4().hex[:8]}"
    for _ in range(MIN_DISTINCT_TENANTS - 1):
        tenant = _make_tenant(db_session, home_metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, home_metro)

    result = compute_benchmark(db_session, canonical_sku.id, home_metro, AS_OF)
    assert result is None


def test_no_single_tenant_derived_number_leaks_through_percentiles(db_session, canonical_sku, distributor):
    """Even with exactly MIN_DISTINCT_TENANTS, every returned percentile is
    computed across the full set — spot-check it isn't secretly just one
    tenant's own price by using visibly distinct prices per tenant.
    """
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    prices = ["1.00", "2.00", "3.00", "4.00", "5.00"]
    for price in prices:
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro, price=price)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is not None
    assert result.p50 == Decimal("3.00")
    assert result.p25 == Decimal("2.00")
    assert result.p75 == Decimal("4.00")
