"""Phase 4c gate, per SPEC.md §7: negotiation sheet ranked by dollars
recoverable, not percentage gap — "a 3% gap on a high-volume SKU above a 40%
gap on a low-volume one."

Also covers the history basis (target = the tenant's own prior p25, no peers
required), which is what makes the sheet non-empty for a customer who has no
benchmark cell yet and may not get one for months.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.analytics.benchmark import MIN_DISTINCT_ACCOUNTS
from app.analytics.negotiation import (
    MIN_ANNUALIZATION_DAYS,
    MIN_HISTORY_OBSERVATIONS,
    NegotiationBasis,
    build_negotiation_sheet,
)
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)

# db_session fixture is shared from conftest.py — it rolls back every commit
# a test makes, unlike a bare SessionLocal().


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


def _add_weekly_history(db, tenant, sku, distributor, metro: str, prices: list[str], qty: str = "10") -> None:
    """One delivery a week, oldest price first, most recent landing on AS_OF —
    so prices[-1] is the "current price" the sheet argues against.
    """
    for weeks_ago, price in enumerate(reversed(prices)):
        _add_observation(
            db, tenant, sku, distributor, metro, price, qty=qty, observed_on=AS_OF - timedelta(days=7 * weeks_ago)
        )


def _seed_peer_cell(db, sku, distributor, metro: str, peer_p25_price: str) -> None:
    """MIN_DISTINCT_ACCOUNTS peer tenants all priced at peer_p25_price, so the
    cell's p25 (and every percentile) is exactly that number — a clean,
    predictable benchmark for the test to assert against.
    """
    for _ in range(MIN_DISTINCT_ACCOUNTS):
        peer = Tenant(name=f"Peer Tenant {uuid.uuid4().hex[:8]}", metro=metro, volume_tier=VolumeTier.under_500k)
        db.add(peer)
        db.commit()
        db.refresh(peer)
        _add_observation(db, peer, sku, distributor, metro, peer_p25_price, qty="1")


# --- ranking ---------------------------------------------------------------


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
    sheet_by_sku = {line.canonical_sku_id: line for line in sheet.lines}

    assert cheese.id in sheet_by_sku and toothpicks.id in sheet_by_sku
    cheese_line = sheet_by_sku[cheese.id]
    toothpicks_line = sheet_by_sku[toothpicks.id]

    assert cheese_line.recoverable_in_window > toothpicks_line.recoverable_in_window
    ranked_ids = [line.canonical_sku_id for line in sheet.lines]
    assert ranked_ids.index(cheese.id) < ranked_ids.index(toothpicks.id)


def test_skus_priced_at_or_below_peer_p25_are_excluded(db_session, distributor, home_tenant):
    """One purchase, so there is no history to fall back to either — auto must
    not invent a line for a SKU with nothing to argue.
    """
    sku = _make_sku(db_session, "Fairly Priced Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "4.50", qty="100")

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    assert sku.id not in {line.canonical_sku_id for line in sheet.lines}


def test_every_line_traces_to_invoice_line_item_ids(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Traceable Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "8.00", qty="50")

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)

    assert line.current_price_line_item_id is not None
    assert len(line.quantity_line_item_ids) >= 1
    assert line.current_price_line_item_id in line.quantity_line_item_ids


def test_sheet_capped_at_top_15(db_session, distributor, home_tenant):
    for i in range(20):
        sku = _make_sku(db_session, f"Overpriced Item {i}")
        _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "1.00")
        _add_observation(db_session, home_tenant, sku, distributor, home_tenant.metro, "2.00", qty=str(10 + i))

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)
    assert len(sheet.lines) == 15


# --- history basis: the day-one sheet, no peers required -------------------


def test_history_basis_produces_a_sheet_with_no_peers_at_all(db_session, distributor, home_tenant):
    """The whole point: a customer whose metro has no other tenants still gets
    a ranked sheet, argued from their own invoices.
    """
    sku = _make_sku(db_session, "Crept Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    assert line.basis == NegotiationBasis.history
    assert line.target_price == Decimal("8.0000")
    assert line.current_price == Decimal("10.0000")
    assert line.peer_account_count is None
    assert line.history_observation_count == 3
    # 4 deliveries x 10 lb, $2.00/lb above the tenant's own prior p25.
    assert line.recoverable_in_window == Decimal("80.0000")


def test_peer_basis_alone_yields_nothing_on_the_same_data(db_session, distributor, home_tenant):
    """Verifies the test above isn't passing for some unrelated reason: the
    identical history produces an empty sheet when only peers count.
    """
    sku = _make_sku(db_session, "Crept Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.peer)
    assert sheet.lines == []


def test_history_target_is_not_dragged_down_by_a_single_promo_price(db_session, distributor, home_tenant):
    """One spot-buy bargain must not become the target the whole line argues
    for — a rep dismisses it as a one-off and takes the sheet's credibility
    with it. The median answers with a price actually paid, routinely.
    """
    sku = _make_sku(db_session, "Promo Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["2.00", "10.00", "10.00", "12.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    # Median of [2, 10, 10]. Neither the $2.00 outlier nor the $6.00 that
    # linear interpolation would have produced for a p25 of the same three.
    assert line.target_price == Decimal("10.00")


def test_history_basis_requires_enough_prior_purchases(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Barely Seen Item")
    prices = ["5.00"] * (MIN_HISTORY_OBSERVATIONS - 1) + ["20.00"]
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, prices)

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)
    assert sku.id not in {line.canonical_sku_id for line in sheet.lines}


def test_current_price_is_excluded_from_the_target_it_is_measured_against(db_session, distributor, home_tenant):
    """Same reason the peer basis excludes the asker's own account: including
    today's overpayment quietly raises the bar it's being judged against.
    """
    sku = _make_sku(db_session, "Self Comparison Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["4.00", "4.00", "10.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    # Median of the three priors [4, 4, 10] is 4.00. Folding the current
    # 10.00 back in would give the median of [4, 4, 10, 10] = 7.00.
    assert line.target_price == Decimal("4.00")


def test_history_lines_at_or_below_their_own_target_are_excluded(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Improving Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["10.00", "10.00", "10.00", "6.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)
    assert sku.id not in {line.canonical_sku_id for line in sheet.lines}


# --- auto: strongest available evidence, per SKU ---------------------------


def test_auto_prefers_the_peer_basis_when_the_cell_clears_suppression(db_session, distributor, home_tenant):
    sku = _make_sku(db_session, "Benchmarked Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.auto)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    assert line.basis == NegotiationBasis.peer
    assert line.target_price == Decimal("5.0000")
    assert line.peer_account_count == MIN_DISTINCT_ACCOUNTS
    assert line.history_observation_count is None


def test_auto_falls_back_per_sku_so_one_sheet_can_carry_both(db_session, distributor, home_tenant):
    benchmarked = _make_sku(db_session, "Benchmarked Item")
    unbenchmarked = _make_sku(db_session, "Unbenchmarked Item")
    _seed_peer_cell(db_session, benchmarked, distributor, home_tenant.metro, "5.00")
    for sku in (benchmarked, unbenchmarked):
        _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.auto)
    by_sku = {line.canonical_sku_id: line for line in sheet.lines}

    assert by_sku[benchmarked.id].basis == NegotiationBasis.peer
    assert by_sku[unbenchmarked.id].basis == NegotiationBasis.history


def test_auto_falls_back_to_history_when_the_tenant_beats_peer_p25_but_crept(db_session, distributor, home_tenant):
    """Beating your peers on a SKU doesn't mean your own price didn't move.

    Deciding the basis on "a peer cell exists" rather than "a peer cell shows
    an overpay" dropped these SKUs entirely, so a SKU could carry an open
    creep alert on the Insights page and be silently absent from the default
    negotiation sheet.
    """
    sku = _make_sku(db_session, "Cheap But Creeping Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "10.00")
    # Still under the $10.00 peer p25, but up 50% on what this tenant used to pay.
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["6.00", "6.00", "6.00", "9.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.auto)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    assert line.basis == NegotiationBasis.history
    assert line.target_price == Decimal("6.0000")
    assert line.peer_account_count is None
    assert line.history_observation_count == 3


def test_auto_still_prefers_peer_when_both_bases_have_something_to_argue(db_session, distributor, home_tenant):
    """Peer is the stronger claim: a rep can't answer it with "that was our
    old price." It must win whenever it is available, not merely be tried first.
    """
    sku = _make_sku(db_session, "Both Bases Item")
    _seed_peer_cell(db_session, sku, distributor, home_tenant.metro, "5.00")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "12.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.auto)

    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    assert line.basis == NegotiationBasis.peer
    assert line.target_price == Decimal("5.0000")  # the $5.00 peer p25, not the $8.00 own median


# --- annualization ---------------------------------------------------------


def test_annualizes_from_the_history_actually_present(db_session, distributor, home_tenant):
    """A full quarter of invoices annualizes at 365/90, not a flat x4."""
    sku = _make_sku(db_session, "Long History Item")
    for weeks_ago in range(13):  # 12 weeks back from AS_OF = an 85-day span
        _add_observation(
            db_session,
            home_tenant,
            sku,
            distributor,
            home_tenant.metro,
            "10.00" if weeks_ago else "12.00",
            observed_on=AS_OF - timedelta(days=7 * weeks_ago),
        )

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    assert sheet.window_days == 7 * 12 + 1
    assert sheet.annualization_factor == (Decimal(365) / sheet.window_days).quantize(Decimal("0.0001"))


def test_a_short_history_is_not_annualized_as_if_it_were_a_full_quarter(db_session, distributor, home_tenant):
    """The bug the flat x4 factor had: a tenant a month in had a month of
    quantity multiplied by 4, understating their real annual exposure on
    exactly the customers who have no peer benchmark to fall back on.
    """
    sku = _make_sku(db_session, "Short History Item")
    _add_weekly_history(db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", "10.00"])

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    assert sheet.window_days == 22  # 3 weeks + 1
    assert sheet.annualization_factor > Decimal(4)
    line = next(l for l in sheet.lines if l.canonical_sku_id == sku.id)
    assert line.annualized_savings == (line.recoverable_in_window * sheet.annualization_factor).quantize(
        Decimal("0.0001")
    )


def test_annualization_denominator_is_floored_for_a_brand_new_tenant(db_session, distributor, home_tenant):
    """Three deliveries in one week must not be extrapolated 100x."""
    sku = _make_sku(db_session, "Brand New Item")
    for days_ago in (3, 2, 1, 0):
        _add_observation(
            db_session,
            home_tenant,
            sku,
            distributor,
            home_tenant.metro,
            "20.00" if days_ago == 0 else "10.00",
            observed_on=AS_OF - timedelta(days=days_ago),
        )

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    assert sheet.window_days == 4
    assert sheet.annualization_factor == (Decimal(365) / MIN_ANNUALIZATION_DAYS).quantize(Decimal("0.0001"))


def test_empty_history_returns_an_empty_sheet_rather_than_dividing_by_zero(db_session, home_tenant):
    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF)

    assert sheet.lines == []
    assert sheet.window_days == 0
    assert sheet.total_annualized_savings == Decimal("0.0000")


def test_total_is_the_sum_of_the_lines_shown(db_session, distributor, home_tenant):
    for i in range(3):
        sku = _make_sku(db_session, f"Summed Item {i}")
        _add_weekly_history(
            db_session, home_tenant, sku, distributor, home_tenant.metro, ["8.00", "8.00", "8.00", str(10 + i) + ".00"]
        )

    sheet = build_negotiation_sheet(db_session, home_tenant.id, AS_OF, NegotiationBasis.history)

    assert len(sheet.lines) == 3
    assert sheet.total_annualized_savings == sum(
        (line.annualized_savings for line in sheet.lines), Decimal(0)
    ).quantize(Decimal("0.0001"))
