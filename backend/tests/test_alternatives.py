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
from app.models import Distributor, PriceAlert, PriceObservation
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import AlertStatus, AlertType

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
