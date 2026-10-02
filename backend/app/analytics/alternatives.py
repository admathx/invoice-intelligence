"""Where else a product whose price went up can be bought for less.

A price alert said what rose, by how much, and where that price sits among
similar businesses. It never said what to do instead of paying it. This
finds the same product at another distributor, for less:

- one this location already buys it from, at what it pays there now (its
  own invoices, so nothing here is anyone else's). "Now" as the alert means
  it: the middle of its last few prices, so the two are like for like, and a
  distributor whose own price has just gone up isn't offered at last
  quarter's;
- one it doesn't, at what the businesses that do buy it there typically pay.

The second is other businesses' prices, so it is published under the
benchmark's rule and by the benchmark's own code (app/analytics/benchmark.py
account_prices): a figure needs MIN_DISTINCT_ACCOUNTS independent businesses
behind it, the asker's own business is left out, and it is one number, the
middle one. Splitting a cell by distributor makes it narrower, never easier
to publish: each distributor's cell has to clear the count by itself. A
distributor one business added for itself (a local vendor) is never named
to another.

Only the same catalog product, never a "similar" one. Which products can
stand in for which is a cook's judgment: by name, drumsticks, wings and a
whole chicken are all kinds of chicken, and a 14-inch pizza box is the
nearest thing to a 16-inch one.
"""
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.benchmark import LOOKBACK_DAYS, account_prices, own_price, typical_price
from app.analytics.price_creep import RECENT_WINDOW_SIZE
from app.models import Distributor, PriceAlert, PriceObservation, Tenant
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.tenant import account_key_column

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase4_analytics"]["alternatives"]
MIN_SAVING = Decimal(str(_thresholds["min_saving_pct"]))
MAX_SHOWN = _thresholds["max_shown"]
_PCT_PLACES = Decimal("0.0001")


@dataclass
class Alternative:
    distributor_id: uuid.UUID
    distributor_name: str
    # Per base unit, as the alert's own prices are.
    price: Decimal
    # This location's own price there; otherwise what others typically pay.
    yours: bool
    # How much less than the price the alert flagged, 0..1.
    saving_pct: Decimal = Decimal(0)
    # When this location last bought it there.
    last_bought: date | None = None
    # How many businesses the typical price is taken from, and where.
    distinct_account_count: int | None = None
    scope: str | None = None  # "metro" | "national"


def find_alternatives(
    db: Session, tenant: Tenant, alerts: Sequence[PriceAlert], exclude_account_key: uuid.UUID
) -> dict[uuid.UUID, list[Alternative]]:
    """For each alert (by id), the distributors its product costs less at,
    cheapest first, at most MAX_SHOWN. Over the same window the alert's
    benchmark uses: the LOOKBACK_DAYS up to the alert's own last day.

    One query for the page, not one per alert. An alert from before alerts
    had a distributor has none: there is no telling which one is "another".
    """
    alerts = [alert for alert in alerts if alert.distributor_id is not None]
    if not alerts:
        return {}
    window = timedelta(days=LOOKBACK_DAYS)
    account_key = account_key_column().label("account_key")
    rows = db.execute(
        select(
            PriceObservation.canonical_sku_id,
            PriceObservation.distributor_id,
            PriceObservation.tenant_id,
            account_key,
            PriceObservation.metro,
            PriceObservation.observed_on,
            PriceObservation.unit_price_base,
        )
        .join(Tenant, Tenant.id == PriceObservation.tenant_id)
        .where(
            PriceObservation.canonical_sku_id.in_({alert.canonical_sku_id for alert in alerts}),
            PriceObservation.observed_on >= min(alert.window_end for alert in alerts) - window,
            PriceObservation.observed_on <= max(alert.window_end for alert in alerts),
        )
    ).all()
    by_product = defaultdict(list)
    for row in rows:
        by_product[row.canonical_sku_id].append(row)
    # Who may be named: 'other' is nobody, and a distributor a business added
    # for itself is that business's, to it alone (below).
    distributors = {
        d.id: d
        for d in db.scalars(select(Distributor).where(Distributor.id.in_({row.distributor_id for row in rows})))
        if d.slug != UNRECOGNIZED_SLUG
    }

    found: dict[uuid.UUID, list[Alternative]] = {}
    for alert in alerts:
        mine: dict[uuid.UUID, list[tuple[date, Decimal]]] = defaultdict(list)
        nearby: dict[uuid.UUID, dict[uuid.UUID, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
        anywhere: dict[uuid.UUID, dict[uuid.UUID, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
        for row in by_product[alert.canonical_sku_id]:
            distributor = distributors.get(row.distributor_id)
            if (
                distributor is None
                or row.distributor_id == alert.distributor_id
                or not alert.window_end - window <= row.observed_on <= alert.window_end
            ):
                continue
            if row.tenant_id == tenant.id:
                mine[row.distributor_id].append((row.observed_on, row.unit_price_base))
            elif row.account_key != exclude_account_key and distributor.account_key is None:
                # Another business (never a sister location: its prices are
                # the asker's own company's), at a distributor anyone can use.
                anywhere[row.distributor_id][row.account_key].append(row.unit_price_base)
                if row.metro == tenant.metro:
                    nearby[row.distributor_id][row.account_key].append(row.unit_price_base)

        offers: list[Alternative] = []
        for distributor_id in mine.keys() | anywhere.keys():
            name = distributors[distributor_id].name
            if distributor_id in mine:
                # What this location pays there says more than what others do.
                latest = sorted(mine[distributor_id])[-RECENT_WINDOW_SIZE:]
                offer = Alternative(
                    distributor_id, name, own_price([price for _, price in latest]),
                    yours=True, last_bought=latest[-1][0],
                )  # fmt: skip
            else:
                # The area first, then everywhere; never anything narrower.
                scope, prices = "metro", account_prices(nearby[distributor_id])
                if prices is None:
                    scope, prices = "national", account_prices(anywhere[distributor_id])
                if prices is None:
                    continue
                offer = Alternative(
                    distributor_id, name, typical_price(prices),
                    yours=False, distinct_account_count=len(prices), scope=scope,
                )  # fmt: skip
            if offer.price <= alert.current_price * (1 - MIN_SAVING):
                offer.saving_pct = ((alert.current_price - offer.price) / alert.current_price).quantize(_PCT_PLACES)
                offers.append(offer)
        if offers:
            found[alert.id] = sorted(offers, key=lambda o: (o.price, o.distributor_name))[:MAX_SHOWN]
    return found
