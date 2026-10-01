"""Covers upsert_creep_alerts, which had zero call sites anywhere in the repo
per code review on Phase 4 — specifically the dedup-by-open-alert-per-
canonical_sku_id logic that assumes two open creep alerts never coexist for
the same SKU.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.analytics.price_creep import RECENT_WINDOW_SIZE, upsert_creep_alerts
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation, Tenant
from app.models.enums import AlertStatus, AlertType, BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Creep Alert Test Distributor", slug=f"creep-alert-test-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


@pytest.fixture()
def tenant(db_session):
    t = Tenant(name=f"Creep Alert Test Tenant {uuid.uuid4().hex[:8]}", metro="creep-test-metro", volume_tier=VolumeTier.under_500k)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def canonical_sku(db_session):
    sku = CanonicalSku(name=f"Creep Alert Test SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def _add_observation(db, tenant, sku, distributor, observed_on: date, price: str) -> None:
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
            raw_description="Creep Alert Test Line",
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
    db.add(
        PriceObservation(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            observed_on=observed_on,
            unit_price_base=Decimal(price),
            metro=tenant.metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=line_item_id,
        )
    )
    db.commit()


def _seed_creep(db, tenant, sku, distributor) -> None:
    for i in range(8):
        _add_observation(db, tenant, sku, distributor, AS_OF - timedelta(weeks=13 - i), "10.00")
    for i in range(RECENT_WINDOW_SIZE):
        _add_observation(db, tenant, sku, distributor, AS_OF - timedelta(weeks=RECENT_WINDOW_SIZE - 1 - i), "15.00")


def test_upsert_creates_one_open_alert(db_session, tenant, canonical_sku, distributor):
    _seed_creep(db_session, tenant, canonical_sku, distributor)

    alerts = upsert_creep_alerts(db_session, tenant.id)

    assert len(alerts) == 1
    assert alerts[0].canonical_sku_id == canonical_sku.id
    assert alerts[0].alert_type == AlertType.creep
    assert alerts[0].status == AlertStatus.open
    assert alerts[0].current_price == Decimal("15.0000")


def test_rerun_updates_the_existing_alert_instead_of_duplicating(db_session, tenant, canonical_sku, distributor):
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    first_run = upsert_creep_alerts(db_session, tenant.id)
    assert len(first_run) == 1
    first_alert_id = first_run[0].id

    # A further price rise, then rerun — must update the same open alert row,
    # never create a second open creep alert for the same canonical_sku_id.
    _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF + timedelta(days=1), "20.00")
    second_run = upsert_creep_alerts(db_session, tenant.id)

    assert len(second_run) == 1
    assert second_run[0].id == first_alert_id

    open_alerts = list(
        db_session.scalars(
            select(PriceAlert).where(
                PriceAlert.tenant_id == tenant.id,
                PriceAlert.canonical_sku_id == canonical_sku.id,
                PriceAlert.alert_type == AlertType.creep,
                PriceAlert.status == AlertStatus.open,
            )
        )
    )
    assert len(open_alerts) == 1


def test_an_alert_resolves_when_its_creep_is_gone(db_session, tenant, canonical_sku, distributor):
    """Alerts used to be add-or-update only, so one outlived its cause forever.

    The concrete case is reopen_line_item: it deletes the disputed
    observations and recomputes alerts, but an alert raised by them stayed
    open. Deleting the observations behind the creep reproduces that exactly.
    """
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    [alert] = upsert_creep_alerts(db_session, tenant.id)

    db_session.execute(
        delete(PriceObservation).where(
            PriceObservation.tenant_id == tenant.id, PriceObservation.unit_price_base == Decimal("15.00")
        )
    )
    db_session.commit()
    assert upsert_creep_alerts(db_session, tenant.id) == []

    db_session.refresh(alert)
    assert alert.status == AlertStatus.resolved


def test_resolving_leaves_alerts_a_person_already_handled_alone(db_session, tenant, canonical_sku, distributor):
    """Only `open` alerts are the system's to resolve. An acknowledged or
    dismissed one reflects a decision someone made and keeps its status.
    """
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    [alert] = upsert_creep_alerts(db_session, tenant.id)
    alert.status = AlertStatus.dismissed
    db_session.commit()

    db_session.execute(
        delete(PriceObservation).where(
            PriceObservation.tenant_id == tenant.id, PriceObservation.unit_price_base == Decimal("15.00")
        )
    )
    db_session.commit()
    upsert_creep_alerts(db_session, tenant.id)

    db_session.refresh(alert)
    assert alert.status == AlertStatus.dismissed


def test_a_price_coming_down_is_not_an_alert(db_session, tenant, canonical_sku, distributor):
    """Every place alerts are shown presents them as increases: a drop used to
    open one and read as '▲ -33%' under 'price increases'."""
    for i in range(8):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF - timedelta(weeks=13 - i), "15.00")
    for i in range(RECENT_WINDOW_SIZE):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF - timedelta(weeks=RECENT_WINDOW_SIZE - 1 - i), "10.00")
    assert upsert_creep_alerts(db_session, tenant.id) == []


def test_a_dismissed_alert_stays_away_unless_the_price_climbs_further(db_session, tenant, canonical_sku, distributor):
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    (alert,) = upsert_creep_alerts(db_session, tenant.id)
    alert.status = AlertStatus.dismissed
    db_session.commit()

    # Another delivery at the same price: still dealt with.
    _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF + timedelta(days=1), "15.00")
    assert upsert_creep_alerts(db_session, tenant.id) == []

    # Clearly higher than when it was dismissed: news again.
    for day in range(2, 2 + RECENT_WINDOW_SIZE):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF + timedelta(days=day), "18.00")
    (reopened,) = upsert_creep_alerts(db_session, tenant.id)
    assert reopened.id != alert.id and reopened.current_price == Decimal("18.0000")


def test_each_distributors_prices_are_compared_on_their_own(db_session, tenant, canonical_sku, distributor):
    """The test set's hot cups: Sysco's price rose, and a second distributor
    selling the same cups for less, bought over the same recent weeks, hid
    it when their prices were pooled."""
    cheaper = Distributor(name="Creep Alert Cheaper Distributor", slug=f"creep-alert-cheap-{uuid.uuid4().hex[:8]}")
    db_session.add(cheaper)
    db_session.commit()
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    for i in range(RECENT_WINDOW_SIZE + 1):
        _add_observation(db_session, tenant, canonical_sku, cheaper, AS_OF - timedelta(weeks=RECENT_WINDOW_SIZE - i, days=1), "9.00")

    [alert] = upsert_creep_alerts(db_session, tenant.id)

    assert alert.distributor_id == distributor.id
    assert alert.current_price == Decimal("15.0000")


def test_buying_once_from_a_pricier_distributor_is_not_an_increase(db_session, tenant, canonical_sku, distributor):
    pricier = Distributor(name="Creep Alert Pricier Distributor", slug=f"creep-alert-dear-{uuid.uuid4().hex[:8]}")
    db_session.add(pricier)
    db_session.commit()
    for i in range(13):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF - timedelta(weeks=13 - i), "10.00")
    for i in range(3):
        _add_observation(db_session, tenant, canonical_sku, pricier, AS_OF - timedelta(days=i), "14.00")

    assert upsert_creep_alerts(db_session, tenant.id) == []


def test_an_alert_from_before_distributors_is_replaced_by_a_per_distributor_one(
    db_session, tenant, canonical_sku, distributor
):
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    legacy_opened = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    legacy = PriceAlert(
        id=uuid.uuid4(), tenant_id=tenant.id, canonical_sku_id=canonical_sku.id, alert_type=AlertType.creep,
        baseline_price=Decimal("10"), current_price=Decimal("15"), pct_change=Decimal("0.5"),
        window_start=AS_OF, window_end=AS_OF, status=AlertStatus.open,
        created_at=legacy_opened,
    )  # fmt: skip
    db_session.add(legacy)
    db_session.commit()

    [alert] = upsert_creep_alerts(db_session, tenant.id)

    db_session.refresh(legacy)
    assert legacy.status == AlertStatus.resolved
    assert alert.id != legacy.id and alert.distributor_id == distributor.id
    # Not news: dated as the alert it replaces, so no email announces it again.
    assert alert.created_at == legacy_opened


def test_a_rise_across_only_six_invoices_is_caught(db_session, tenant, canonical_sku, distributor):
    """A second distributor often has a short history. US Foods eggs rising
    12% across six invoices went unflagged for want of eight prices."""
    for i, price in enumerate(["32.27", "33.04", "33.82", "34.59", "35.37", "36.14"]):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF - timedelta(weeks=5 - i), price)

    [alert] = upsert_creep_alerts(db_session, tenant.id)

    assert alert.baseline_price == Decimal("33.0400") and alert.current_price == Decimal("35.3700")


def test_six_steady_invoices_or_five_of_anything_raise_nothing(db_session, tenant, canonical_sku, distributor):
    for i, price in enumerate(["33.00", "33.40", "32.80", "33.10", "33.30", "32.90"]):
        _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF - timedelta(weeks=5 - i), price)
    assert upsert_creep_alerts(db_session, tenant.id) == []

    other = CanonicalSku(name=f"Creep Alert Short SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(other)
    db_session.commit()
    for i, price in enumerate(["10.00", "10.00", "13.00", "13.00", "13.00"]):  # five: too few to judge
        _add_observation(db_session, tenant, other, distributor, AS_OF - timedelta(weeks=4 - i), price)
    assert upsert_creep_alerts(db_session, tenant.id) == []


# --- What the fourth test set showed -----------------------------------------


def _series(db, tenant, sku, distributor, prices: list[str]) -> list[PriceAlert]:
    for week, price in enumerate(prices):
        _add_observation(db, tenant, sku, distributor, AS_OF + timedelta(weeks=week), price)
    db.commit()
    return upsert_creep_alerts(db, tenant.id)


AVOCADOS = ["1.259", "1.486", "1.032", "1.536", "0.982", "1.448", "1.108", "1.562", "1.007", "1.385", "1.158", "1.511", "1.07", "1.284"]  # fmt: skip


@pytest.mark.parametrize("weeks", range(6, len(AVOCADOS) + 1))
def test_produce_that_swings_every_week_is_not_creeping(db_session, tenant, canonical_sku, distributor, weeks):
    """Avocados a quarter up or down from week to week opened and closed an
    alert eight times in fourteen weeks, whenever the dear weeks happened to
    sit in the recent window."""
    assert _series(db_session, tenant, canonical_sku, distributor, AVOCADOS[:weeks]) == []


def test_a_price_that_stepped_up_and_stayed_is_still_an_increase(db_session, tenant, canonical_sku, distributor):
    """The swing rule is about prices that come back down. One that went up
    15% and stayed there has not, however long ago it rose."""
    alerts = _series(db_session, tenant, canonical_sku, distributor, ["2.905"] * 5 + ["3.34"] * 9)
    assert len(alerts) == 1 and alerts[0].current_price == Decimal("3.34")


def test_one_earlier_spike_does_not_hide_a_later_rise(db_session, tenant, canonical_sku, distributor):
    prices = ["4.00", "4.00", "5.20", "4.00", "4.00", "4.00", "4.00", "4.00", "4.40", "4.40", "4.40", "4.40", "4.40"]
    assert len(_series(db_session, tenant, canonical_sku, distributor, prices)) == 1


def test_a_steady_climb_across_six_invoices_is_caught_by_its_trend(db_session, tenant, canonical_sku, distributor):
    """Mozzarella from a second distributor, up 19% over six invoices: the
    newest three against the oldest three measured 4.7%, under the bar."""
    alerts = _series(db_session, tenant, canonical_sku, distributor, ["3.2155", "3.4155", "3.5175", "3.478", "3.576", "3.8245"])
    assert len(alerts) == 1


def test_two_odd_prices_in_a_short_history_are_not_a_trend(db_session, tenant, canonical_sku, distributor):
    """A spot buy at half again the price, and the two after it a little up:
    not creep, and the trend rule mustn't make it so."""
    prices = ["20.2765", "20.3185", "20.642", "20.4023", "29.9389", "21.2532", "21.1313"]
    assert _series(db_session, tenant, canonical_sku, distributor, prices) == []


def test_a_small_rise_across_six_invoices_waits_for_more(db_session, tenant, canonical_sku, distributor):
    prices = ["0.0659", "0.0652", "0.0685", "0.0684", "0.069", "0.071"]
    assert _series(db_session, tenant, canonical_sku, distributor, prices) == []
