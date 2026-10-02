"""A year of buying at today's prices, and what would change it
(app/analytics/costs.py)."""
import uuid
from datetime import timedelta
from decimal import Decimal

from app.analytics.costs import build_costs
from app.db import bind_tenant
from app.models import Invoice, InvoiceLineItem, PriceAlert
from app.models.enums import AlertStatus, AlertType, InvoiceSource, InvoiceStatus, ReviewStatus

from test_alternatives import _a_quarter_at, _bought, _distributor, _metro, _others_pay  # noqa: F401
from test_suppression import AS_OF, _make_tenant, canonical_sku, distributor  # noqa: F401

TODAY = AS_OF + timedelta(days=30)  # a month after the last invoice


def _costs(db, tenant):
    return build_costs(db, tenant, TODAY)


def _scenario(costs, key):
    return next(s for s in costs.scenarios if s.key == key)


def _open_alert(db, tenant, sku, distributor, *, before="9.00", now="10.00") -> None:
    bind_tenant(db, tenant.id)
    db.add(
        PriceAlert(
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            alert_type=AlertType.creep,
            baseline_price=Decimal(before),
            current_price=Decimal(now),
            pct_change=Decimal("0.1111"),
            window_start=AS_OF - timedelta(days=60),
            window_end=AS_OF,
            status=AlertStatus.open,
        )
    )
    db.commit()


def test_a_location_with_no_invoices_has_nothing_to_plan_with(db_session):
    costs = _costs(db_session, _make_tenant(db_session, _metro()))

    assert (costs.yearly_cost, costs.products, costs.scenarios) == (Decimal("0.00"), [], [])
    assert costs.window_end is None and costs.coverage is None


def test_a_year_is_what_was_bought_at_what_it_costs_now(db_session, canonical_sku, distributor):
    """Seven deliveries of 10 lb over 73 days is 350 lb a year. The price
    was $9 and is $10 now: the year is priced at $10, which is what the
    next one will be bought at."""
    tenant = _make_tenant(db_session, _metro())
    for days_ago, price in ((72, "9.00"), (60, "9.00"), (48, "10.00"), (36, "10.00"), (24, "10.00"), (12, "10.00"), (0, "10.00")):
        _bought(db_session, tenant, canonical_sku, distributor, price, days_ago=days_ago)

    costs = _costs(db_session, tenant)

    [product] = costs.products
    assert (product.name, product.category, product.distributor) == (canonical_sku.name, "test", distributor.name)
    assert product.yearly_cost == costs.yearly_cost == Decimal("3500.00")
    # Counted back from its own last invoice, not from today.
    assert (costs.window_end, costs.window_days) == (AS_OF, 73)


def test_each_products_recent_move_is_measured_and_can_be_played_again(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    for days_ago, price in ((72, "10.00"), (60, "10.00"), (48, "10.00"), (24, "11.00"), (12, "11.00"), (0, "11.00")):
        _bought(db_session, tenant, canonical_sku, distributor, price, days_ago=days_ago)

    costs = _costs(db_session, tenant)

    assert costs.products[0].recent_change == Decimal("0.1000")
    # 300 lb a year at $11 is $3,300; another 10% on that is $330.
    again = _scenario(costs, "rise_again")
    assert (costs.yearly_cost, again.yearly_change, again.products) == (Decimal("3300.00"), Decimal("330.00"), 1)


def test_a_product_bought_once_shows_no_move(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _bought(db_session, tenant, canonical_sku, distributor, "10.00")

    costs = _costs(db_session, tenant)

    assert costs.products[0].recent_change is None
    assert _scenario(costs, "rise_again").yearly_change == Decimal("0.00")


def test_reversing_the_flagged_increases_is_worth_the_rise_times_a_years_buying(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)  # 350 lb a year at $10
    _open_alert(db_session, tenant, canonical_sku, distributor, before="9.00", now="10.00")

    reversed_ = _scenario(_costs(db_session, tenant), "increases_reversed")

    assert (reversed_.yearly_change, reversed_.products) == (Decimal("-350.00"), 1)


def test_an_alert_about_something_no_longer_bought_moves_nothing(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)
    _open_alert(db_session, tenant, canonical_sku, _distributor(db_session, "Long Gone"))

    reversed_ = _scenario(_costs(db_session, tenant), "increases_reversed")

    assert (reversed_.yearly_change, reversed_.products) == (Decimal("0.00"), 0)


def test_the_cheapest_price_found_elsewhere_is_a_scenario_of_its_own(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)
    _others_pay(db_session, canonical_sku, _distributor(db_session, "Elsewhere Foods"), metro, ["8.50"] * 5)
    _open_alert(db_session, tenant, canonical_sku, distributor)

    elsewhere = _scenario(_costs(db_session, tenant), "cheaper_elsewhere")

    # $1.50 less on 350 lb a year, as the alert itself says.
    assert (elsewhere.yearly_change, elsewhere.products) == (Decimal("-525.00"), 1)


def test_what_the_total_leaves_out_is_said(db_session, canonical_sku, distributor):
    """Items not matched to a product have no price per unit to move. The
    total is of the rest, and says how much of the spending that is."""
    tenant = _make_tenant(db_session, _metro())
    _bought(db_session, tenant, canonical_sku, distributor, "10.00")  # $100, matched
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant.id, distributor_id=distributor.id, invoice_date=AS_OF, total=Decimal("300"),
        source=InvoiceSource.upload, original_file_uri="file:///dev/null", status=InvoiceStatus.extracted,
    )  # fmt: skip
    db_session.add(invoice)
    db_session.add(
        InvoiceLineItem(
            tenant_id=tenant.id, invoice_id=invoice.id, line_number=1, raw_description="NOT MATCHED", quantity=Decimal("1"),
            unit_price=Decimal("300"), extended_price=Decimal("300"), uom="CS", review_status=ReviewStatus.pending,
        )  # fmt: skip
    )
    db_session.commit()

    assert _costs(db_session, tenant).coverage == Decimal("0.2500")
