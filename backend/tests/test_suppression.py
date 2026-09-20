"""Phase 4b gate, per SPEC.md §7: "Suppress the cell entirely if it has fewer
than 5 distinct tenants — this is a hard rule, not a tunable." Written before
any benchmark computation touches real corpus data — a suppression bug here
is a privacy leak (an identifiable competitor's price), not just a wrong
number.

The rule is enforced over distinct *accounts*: a tenant is a location, so
counting tenants lets one multi-unit business clear its own threshold with its
own locations. The account tests at the bottom cover that directly.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.analytics.benchmark import MIN_DISTINCT_ACCOUNTS, account_key_for, compute_benchmark
from app.models import Account, CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)

# db_session fixture is shared from conftest.py — it rolls back every commit
# a test makes, unlike a bare SessionLocal().


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


def _make_tenant(
    db, metro: str, volume_tier: VolumeTier = VolumeTier.under_500k, account: Account | None = None
) -> Tenant:
    tenant = Tenant(
        name=f"Suppression Test Tenant {uuid.uuid4().hex[:8]}",
        metro=metro,
        volume_tier=volume_tier,
        account_id=account.id if account is not None else None,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _make_account(db, name: str = "Group") -> Account:
    account = Account(name=f"{name} {uuid.uuid4().hex[:8]}")
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


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
    for _ in range(MIN_DISTINCT_ACCOUNTS - 1):
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is None


def test_five_distinct_tenants_returns_a_result(db_session, canonical_sku, distributor):
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    for _ in range(MIN_DISTINCT_ACCOUNTS):
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is not None
    assert result.scope == "metro"
    assert result.distinct_account_count == MIN_DISTINCT_ACCOUNTS


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
    assert result.distinct_account_count == 5


def test_national_also_suppressed_returns_none(db_session, canonical_sku, distributor):
    home_metro = f"metro-{uuid.uuid4().hex[:8]}"
    for _ in range(MIN_DISTINCT_ACCOUNTS - 1):
        tenant = _make_tenant(db_session, home_metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, home_metro)

    result = compute_benchmark(db_session, canonical_sku.id, home_metro, AS_OF)
    assert result is None


def test_no_single_tenant_derived_number_leaks_through_percentiles(db_session, canonical_sku, distributor):
    """Even with exactly MIN_DISTINCT_ACCOUNTS, every returned percentile is
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


# --- accounts: a tenant is a location, an account is a business -------------


def test_one_group_with_five_locations_is_suppressed(db_session, canonical_sku, distributor):
    """The bug this table exists to prevent: five tenants clear a
    five-*tenant* threshold, but they are one business, so the "peer
    benchmark" would be that business compared against itself.
    """
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    group = _make_account(db_session, "Five Location Group")
    for _ in range(MIN_DISTINCT_ACCOUNTS):
        tenant = _make_tenant(db_session, metro, account=group)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is None


def test_a_group_counts_once_toward_the_threshold(db_session, canonical_sku, distributor):
    """Three independents plus a two-location group is five tenants but four
    businesses — still suppressed. Adding one more independent (five
    businesses) clears it.
    """
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    group = _make_account(db_session, "Two Location Group")
    for _ in range(2):
        tenant = _make_tenant(db_session, metro, account=group)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)
    for _ in range(3):
        tenant = _make_tenant(db_session, metro)
        _add_observation(db_session, tenant, canonical_sku, distributor, metro)

    assert compute_benchmark(db_session, canonical_sku.id, metro, AS_OF) is None

    _add_observation(db_session, _make_tenant(db_session, metro), canonical_sku, distributor, metro)
    result = compute_benchmark(db_session, canonical_sku.id, metro, AS_OF)
    assert result is not None
    assert result.distinct_account_count == MIN_DISTINCT_ACCOUNTS


def test_excluding_the_asker_excludes_its_sibling_locations(db_session, canonical_sku, distributor):
    """Excluding only the asking location still leaves its siblings' prices in
    the cell it is compared against — which are its own company's prices.
    """
    metro = f"metro-{uuid.uuid4().hex[:8]}"
    group = _make_account(db_session, "Asking Group")
    asker = _make_tenant(db_session, metro, account=group)
    _add_observation(db_session, asker, canonical_sku, distributor, metro, price="9.00")
    for _ in range(4):
        sibling = _make_tenant(db_session, metro, account=group)
        _add_observation(db_session, sibling, canonical_sku, distributor, metro, price="9.00")
    for price in ["1.00", "2.00", "3.00", "4.00", "5.00"]:
        peer = _make_tenant(db_session, metro)
        _add_observation(db_session, peer, canonical_sku, distributor, metro, price=price)

    result = compute_benchmark(
        db_session,
        canonical_sku.id,
        metro,
        AS_OF,
        exclude_account_key=account_key_for(db_session, asker.id),
    )

    assert result is not None
    # Five true peers only: the group's own $9.00 observations are gone, so
    # the percentiles are exactly the five independents' prices.
    assert result.distinct_account_count == 5
    assert result.p25 == Decimal("2.00")
    assert result.p50 == Decimal("3.00")
    assert result.p75 == Decimal("4.00")


def test_account_key_falls_back_to_the_tenant_id_for_independents(db_session):
    """A tenant with no account is its own business — which is what makes this
    change a no-op for every single-location customer.
    """
    tenant = _make_tenant(db_session, f"metro-{uuid.uuid4().hex[:8]}")
    assert account_key_for(db_session, tenant.id) == tenant.id


def test_account_key_is_the_account_for_a_group_member(db_session):
    group = _make_account(db_session, "Keyed Group")
    tenant = _make_tenant(db_session, f"metro-{uuid.uuid4().hex[:8]}", account=group)
    assert account_key_for(db_session, tenant.id) == group.id
