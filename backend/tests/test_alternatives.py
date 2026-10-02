"""Where else a product on a price alert costs less (app/analytics/alternatives.py).

Half of what it shows is other businesses' prices, so half of these are the
privacy rule again: the same count, the same exclusions, per distributor.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.analytics.alternatives import MAX_SHOWN, find_alternatives
from app.analytics.benchmark import LOOKBACK_DAYS, MIN_DISTINCT_ACCOUNTS, account_key_for
from app.analytics.price_creep import RECENT_WINDOW_SIZE
from app.analytics.switching import WORTH_A_NEW_SUPPLIER, WORTH_MOVING, weighed_alternatives
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import AlertStatus, AlertType, BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus

from test_suppression import (  # noqa: F401  (fixtures and row builders)
    AS_OF,
    _make_account,
    _make_line_item,
    _make_tenant,
    canonical_sku,
    distributor,
)


def _distributor(db, name: str, *, account_key=None, slug: str | None = None) -> Distributor:
    d = Distributor(name=name, slug=slug or f"alt-{uuid.uuid4().hex[:8]}", account_key=account_key)
    db.add(d)
    db.commit()
    return d


def _paid(db, tenant, sku, distributor, price: str, *, days_ago: int = 10) -> None:
    observed_on = AS_OF - timedelta(days=days_ago)
    db.add(
        PriceObservation(
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            observed_on=observed_on,
            unit_price_base=Decimal(price),
            metro=tenant.metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=_make_line_item(db, tenant, sku, distributor, observed_on, price),
        )
    )
    db.commit()


def _alert(db, tenant, sku, distributor, current: str = "10.00") -> PriceAlert:
    """Not saved: what's asked about an alert is in the alert itself."""
    return PriceAlert(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        canonical_sku_id=sku.id,
        distributor_id=distributor.id if distributor else None,
        alert_type=AlertType.creep,
        baseline_price=Decimal("9.00"),
        current_price=Decimal(current),
        pct_change=Decimal("0.1111"),
        window_start=AS_OF - timedelta(days=60),
        window_end=AS_OF,
        status=AlertStatus.open,
    )


def _others_pay(db, sku, distributor, metro: str, prices: list[str]) -> None:
    """Each price is a different business's."""
    for price in prices:
        _paid(db, _make_tenant(db, metro), sku, distributor, price)


def _found(db, tenant, alert):
    return find_alternatives(db, tenant, [alert], account_key_for(db, tenant.id)).get(alert.id, [])


def _metro() -> str:
    return f"metro-{uuid.uuid4().hex[:8]}"


# --- your own price somewhere else --------------------------------------------


def test_a_distributor_you_already_buy_it_from_for_less_is_offered(db_session, canonical_sku, distributor):
    """Sysco's price went up; the same product has been coming from US Foods
    for less. The alert said what rose and never where it costs less."""
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    for price, days_ago in (("8.40", 40), ("8.50", 20), ("8.60", 5)):
        _paid(db_session, tenant, canonical_sku, elsewhere, price, days_ago=days_ago)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _found(db_session, tenant, alert)

    assert (offer.distributor_name, offer.yours) == ("Elsewhere Foods", True)
    assert offer.price == Decimal("8.50"), "the middle of its last few prices there, not one spot buy"
    assert offer.saving_pct == Decimal("0.1500")
    assert offer.last_bought == AS_OF - timedelta(days=5)
    assert offer.distinct_account_count is None, "its own invoices: nobody else's price is in it"


def test_somewhere_barely_cheaper_or_dearer_is_not_an_alternative(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _paid(db_session, tenant, canonical_sku, _distributor(db_session, "A Shade Less"), "9.80")  # 2% less
    _paid(db_session, tenant, canonical_sku, _distributor(db_session, "Dearer"), "11.00")
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_a_distributor_whose_own_price_just_went_up_is_judged_on_the_new_one(db_session, canonical_sku, distributor):
    """It was $7 there all quarter and is $9.90 now. Offered at the quarter's
    middle price, it would be a saving that no longer exists."""
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    for week in range(RECENT_WINDOW_SIZE + 2):
        _paid(db_session, tenant, canonical_sku, elsewhere, "7.00", days_ago=80 - week)
    for week in range(RECENT_WINDOW_SIZE):
        _paid(db_session, tenant, canonical_sku, elsewhere, "9.90", days_ago=30 - week)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_only_what_was_paid_lately_counts(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _paid(db_session, tenant, canonical_sku, _distributor(db_session, "Long Ago"), "5.00", days_ago=LOOKBACK_DAYS + 1)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_the_alerts_own_distributor_is_never_its_alternative(db_session, canonical_sku, distributor):
    """The price it used to charge is cheaper than the one it charges now."""
    tenant = _make_tenant(db_session, _metro())
    _paid(db_session, tenant, canonical_sku, distributor, "8.00", days_ago=50)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_an_alert_with_no_distributor_has_no_alternatives(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    _paid(db_session, tenant, canonical_sku, distributor, "8.00")
    alert = _alert(db_session, tenant, canonical_sku, None)

    assert _found(db_session, tenant, alert) == []


# --- what others pay somewhere you don't buy it ---------------------------------


def test_what_five_businesses_pay_at_another_distributor_is_offered(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _others_pay(db_session, canonical_sku, elsewhere, metro, ["8.00", "8.20", "8.40", "8.60", "9.50"])
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _found(db_session, tenant, alert)

    assert (offer.distributor_name, offer.yours, offer.last_bought) == ("Elsewhere Foods", False, None)
    assert offer.price == Decimal("8.40"), "the middle one: one number, as the benchmark's typical price is"
    assert (offer.distinct_account_count, offer.scope) == (MIN_DISTINCT_ACCOUNTS, "metro")
    assert offer.saving_pct == Decimal("0.1600")


def test_four_businesses_at_a_distributor_are_never_shown(db_session, canonical_sku, distributor):
    """The benchmark's rule, per distributor: a narrower cell is no easier
    to publish. Four is four, however many buy the product elsewhere."""
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    _others_pay(db_session, canonical_sku, _distributor(db_session, "Too Few"), metro, ["5.00"] * (MIN_DISTINCT_ACCOUNTS - 1))
    _others_pay(db_session, canonical_sku, distributor, metro, ["9.00"] * 6)  # plenty, at the alert's own
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_one_group_with_five_locations_is_one_business(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    group = _make_account(db_session)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    for _ in range(MIN_DISTINCT_ACCOUNTS):
        _paid(db_session, _make_tenant(db_session, metro, account=group), canonical_sku, elsewhere, "5.00")
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_your_own_other_locations_are_not_the_others(db_session, canonical_sku, distributor):
    """Four other businesses and a sister location are four businesses."""
    metro = _metro()
    group = _make_account(db_session)
    tenant = _make_tenant(db_session, metro, account=group)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _paid(db_session, _make_tenant(db_session, metro, account=group), canonical_sku, elsewhere, "5.00")
    _others_pay(db_session, canonical_sku, elsewhere, metro, ["5.00"] * (MIN_DISTINCT_ACCOUNTS - 1))
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_a_vendor_another_business_added_for_itself_is_never_named(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    theirs = _distributor(db_session, "Somebody's Own Butcher", account_key=uuid.uuid4())
    _others_pay(db_session, canonical_sku, theirs, metro, ["5.00"] * 6)
    # Nor is 'other', which is where an invoice nobody could attribute sits.
    nobody = db_session.scalar(select(Distributor).where(Distributor.slug == UNRECOGNIZED_SLUG)) or _distributor(
        db_session, "Other", slug=UNRECOGNIZED_SLUG
    )
    _others_pay(db_session, canonical_sku, nobody, metro, ["5.00"] * 6)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    assert _found(db_session, tenant, alert) == []


def test_with_too_few_nearby_it_falls_back_to_everywhere_and_says_so(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _others_pay(db_session, canonical_sku, elsewhere, metro, ["8.00", "8.00"])
    _others_pay(db_session, canonical_sku, elsewhere, _metro(), ["8.00", "8.00", "8.00"])
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _found(db_session, tenant, alert)

    assert (offer.scope, offer.distinct_account_count) == ("national", 5)


def test_your_own_price_there_is_shown_rather_than_what_others_pay(db_session, canonical_sku, distributor):
    """Others get it there for less than you do. What you pay there is the
    alternative you actually have."""
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _paid(db_session, tenant, canonical_sku, elsewhere, "9.00")
    _others_pay(db_session, canonical_sku, elsewhere, metro, ["6.00"] * 5)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _found(db_session, tenant, alert)

    assert (offer.yours, offer.price) == (True, Decimal("9.00"))


def test_the_cheapest_few_come_first(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    for name, price in (("C", "9.00"), ("A", "7.00"), ("D", "9.50"), ("B", "8.00")):
        _paid(db_session, tenant, canonical_sku, _distributor(db_session, name), price)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    offers = _found(db_session, tenant, alert)

    assert [o.distributor_name for o in offers] == ["A", "B", "C"][:MAX_SHOWN]


# --- whether it's worth it (app/analytics/switching.py) ---------------------------


def _bought(db, tenant, sku, distributor, price: str, *, qty: str = "10", days_ago: int = 0, invoice_total: str = "2000") -> None:
    """A delivery: an invoice with a total, and this product on it."""
    observed_on = AS_OF - timedelta(days=days_ago)
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id,
        invoice_date=observed_on,
        total=Decimal(invoice_total),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    line = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        line_number=1,
        raw_description="Switching Test Line",
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        extended_price=Decimal(qty) * Decimal(price),
        uom="LB",
        canonical_sku_id=sku.id,
        normalized_qty_base=Decimal(qty),
        normalized_unit_price=Decimal(price),
        base_uom=sku.base_uom,
        review_status=ReviewStatus.auto,
    )
    db.add_all([invoice, line])
    db.flush()
    db.add(
        PriceObservation(
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            observed_on=observed_on,
            unit_price_base=Decimal(price),
            metro=tenant.metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=line.id,
        )
    )
    db.commit()


def _a_quarter_at(db, tenant, sku, distributor, price: str = "10.00", **delivery) -> None:
    """Seven deliveries over 73 days, 10 lb each: a year is five times that."""
    for days_ago in range(0, 73, 12):
        _bought(db, tenant, sku, distributor, price, days_ago=days_ago, **delivery)


def _weighed(db, tenant, *alerts):
    found = weighed_alternatives(db, tenant, list(alerts), account_key_for(db, tenant.id))
    return [found.get(alert.id, []) for alert in alerts] if len(alerts) > 1 else found.get(alerts[0].id, [])


def test_a_saving_is_put_in_dollars_a_year_at_what_the_location_buys(db_session, canonical_sku, distributor):
    """$1.50 a pound less means nothing until it's multiplied by the pounds."""
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)
    for days_ago in (5, 20, 40):
        _bought(db_session, tenant, canonical_sku, elsewhere, "8.50", qty="1", days_ago=days_ago, invoice_total="500")
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    # 70 lb in 73 days is 350 lb a year, at $1.50 less.
    assert offer.annual_saving == Decimal("525.00")


def test_a_distributor_that_already_delivers_is_worth_moving_a_small_line_to(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    distributor.name = "Main Street Foods"
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)  # $100 of each $2,000 invoice
    for days_ago in (5, 20, 40):
        _bought(db_session, tenant, canonical_sku, elsewhere, "8.50", qty="1", days_ago=days_ago, invoice_total="500")
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    assert offer.advice.verdict == "move"
    assert offer.advice.headline == "Worth moving: Elsewhere Foods already delivers to you."
    said = " ".join(offer.advice.points)
    assert "You already buy from Elsewhere Foods (3 invoices in the last 90 days)" in said
    # Who the order is with, and how much of it this is: $14,000 of $15,500, and $700 of that.
    assert "Main Street Foods is 90% of your spending over the last 90 days." in said
    assert "Elsewhere Foods is 10% of your spending." in said
    assert "This product is about $3,500 a year with Main Street Foods, 5% of what you buy from them." in said
    assert "A small part of your order with them" in said
    assert "Asking Main Street Foods to match is still free" in said


def test_a_few_dollars_a_year_isnt_worth_changing_an_order_for(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    _a_quarter_at(db_session, tenant, canonical_sku, distributor, qty="1")  # 35 lb a year: $52.50
    _bought(db_session, tenant, canonical_sku, elsewhere, "8.50", days_ago=5)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    assert offer.annual_saving == Decimal("52.50") and offer.annual_saving < WORTH_MOVING
    assert offer.advice.verdict == "stay"
    assert offer.advice.headline.startswith("Too small to change an order for.")


def test_a_big_part_of_the_order_is_something_to_ask_them_to_match_first(db_session, canonical_sku, distributor):
    """A restaurant gets its pricing by ordering mostly from one distributor.
    A product that is a fifth of the order isn't moved lightly."""
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    distributor.name = "Main Street Foods"
    _a_quarter_at(db_session, tenant, canonical_sku, distributor, invoice_total="500")  # $100 of each $500
    _bought(db_session, tenant, canonical_sku, elsewhere, "8.50", days_ago=5)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    assert offer.advice.verdict == "negotiate"
    assert offer.advice.headline == (
        "Ask Main Street Foods to match it first: this is 20% of what you buy from them, "
        "and moving it could cost you on the rest."
    )
    assert any("moving it could cost you volume pricing on the rest" in point for point in offer.advice.points)


def test_one_cheaper_product_isnt_worth_a_new_supplier(db_session, canonical_sku, distributor):
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    distributor.name = "Main Street Foods"
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)
    _others_pay(db_session, canonical_sku, elsewhere, metro, ["8.50"] * 5)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    assert offer.annual_saving == Decimal("525.00") and offer.annual_saving < WORTH_A_NEW_SUPPLIER
    assert offer.advice.verdict == "stay"
    assert offer.advice.headline == "Not worth opening a new supplier for. Take this price to your Main Street Foods rep."
    said = " ".join(offer.advice.points)
    # What a new supplier costs, and what ordering from one is worth.
    assert "You don't buy from Elsewhere Foods today." in said and "meeting their minimum on every order" in said
    assert "Main Street Foods is 100% of your spending over the last 90 days." in said
    assert "Ordering mostly from one distributor is usually what earns your pricing" in said
    assert "what 5 businesses typically pay Elsewhere Foods, not a quote" in said
    # $3,500 a year of it: an order that small runs into their minimum.
    assert "On its own, this product is about $67 a week of orders." in said and "ask Elsewhere Foods for theirs" in said


def test_several_flagged_products_cheaper_at_one_supplier_are_weighed_together(db_session, canonical_sku, distributor):
    """Nobody opens an account for one product. Three may be worth a quote."""
    metro = _metro()
    tenant = _make_tenant(db_session, metro)
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    distributor.name = "Main Street Foods"
    second = CanonicalSku(name=f"Switching Test SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(second)
    db_session.commit()
    alerts = []
    for sku in (canonical_sku, second):
        _a_quarter_at(db_session, tenant, sku, distributor)
        _others_pay(db_session, sku, elsewhere, metro, ["8.50"] * 5)
        alerts.append(_alert(db_session, tenant, sku, distributor))

    [[first], [other]] = _weighed(db_session, tenant, *alerts)

    assert first.annual_saving == other.annual_saving == Decimal("525.00")
    assert first.advice.verdict == other.advice.verdict == "negotiate"
    assert first.advice.headline == (
        "Ask Main Street Foods to match it first. If they won't, it's worth a quote from Elsewhere Foods."
    )
    assert "2 of your flagged products cost less at Elsewhere Foods: about $1,050 a year together." in first.advice.points
    assert any(point.startswith("On their own, these products are about $135 a week of orders.") for point in first.advice.points)


def test_an_alternative_links_to_the_distributor(db_session, canonical_sku, distributor):
    tenant = _make_tenant(db_session, _metro())
    elsewhere = _distributor(db_session, "Elsewhere Foods")
    elsewhere.website = "https://elsewhere.example"
    _a_quarter_at(db_session, tenant, canonical_sku, distributor)
    _bought(db_session, tenant, canonical_sku, elsewhere, "8.50", days_ago=5)
    alert = _alert(db_session, tenant, canonical_sku, distributor)

    [offer] = _weighed(db_session, tenant, alert)

    assert offer.website == "https://elsewhere.example"


def test_the_shared_distributors_have_their_websites(db_session):
    websites = dict(db_session.execute(select(Distributor.slug, Distributor.website).where(Distributor.account_key.is_(None))).all())
    assert websites["sysco"] == "https://www.sysco.com" and websites["us_foods"] == "https://www.usfoods.com"
    assert websites["gordon"] and websites["pfg"] and websites.get("other") is None
