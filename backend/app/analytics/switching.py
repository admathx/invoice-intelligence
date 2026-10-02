"""Whether buying a flagged product somewhere cheaper is worth doing.

A lower price at another distributor is not yet a saving. A restaurant
usually gets its pricing by ordering mostly from one distributor: volume is
what the price list, the rebate and the rep's attention are for. Moving a
product away gives some of that up, and a distributor it doesn't buy from
yet costs more to start with: an account, their minimum on every order, one
more delivery to receive and one more invoice to check, every week.

So each alternative (app/analytics/alternatives.py) is weighed here before
it is shown, from what this location's own invoices say:

- what the difference comes to in a year, at what it buys;
- whether the other distributor already delivers to it;
- how much of its spending the current distributor has, and how much of
  that is this product.

and given a verdict a person can act on, with the reasons under it:

- move: the other distributor already delivers, the saving is worth
  changing an order for, and the product is a small part of what the
  current one sells it;
- negotiate: ask the current distributor to match first. Either the
  product is a large part of what it sells this location, or the saving
  would justify a new supplier and a match would get it for nothing;
- stay: too small to act on, or not worth a new supplier. The price is
  still something to take to the rep.

What this can't see is the agreement itself: a rebate tier, a committed
share of purchases, a minimum order. It says so rather than guessing, and
tells the person what to ask. The thresholds are judgment
(validation/thresholds.yaml), not measurement.
"""
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.alternatives import Advice, Alternative, find_alternatives
from app.analytics.benchmark import LOOKBACK_DAYS
from app.analytics.negotiation import annualization
from app.db import bind_tenant
from app.models import Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation, Tenant
from app.models.enums import InvoiceStatus
from app.models.invoice import not_held

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase4_analytics"]["alternatives"]
WORTH_MOVING = Decimal(str(_thresholds["worth_moving_min_usd_per_year"]))
WORTH_A_NEW_SUPPLIER = Decimal(str(_thresholds["worth_new_supplier_min_usd_per_year"]))
LARGE_SHARE = Decimal(str(_thresholds["large_share_of_distributor"]))
_CENT = Decimal("0.01")
WEEKS_PER_YEAR = Decimal(52)


def usd(amount: Decimal) -> str:
    """A yearly figure as it's said: whole dollars. It is an estimate, and
    cents would claim otherwise."""
    return "under $1" if amount < 1 else f"${amount:,.0f}"


def _share(part: Decimal) -> str:
    return "under 1%" if part < Decimal("0.01") else f"{part:.0%}"


@dataclass
class _Standing:
    """What this location's invoices say about the distributor an alert is
    about, over the alert's window."""

    current: str  # its name
    # Of everything the location spent, how much was with it; None when
    # there are no invoice totals to go by.
    share_of_spending: Decimal | None
    # Of what was spent with it, how much was this product.
    product_share: Decimal | None
    # What the product comes to in a year there.
    product_a_year: Decimal | None


@dataclass
class _Flagged:
    """Every flagged product that costs less at one distributor this
    location doesn't buy from: how many, what they'd save in a year between
    them, and what the location spends on them in a year now."""

    products: int = 0
    saving: Decimal = Decimal(0)
    spent: Decimal = Decimal(0)


def _advise(
    offer: Alternative, standing: _Standing, their_share: Decimal | None, deliveries: int, flagged: _Flagged
) -> Advice:
    """`their_share` and `deliveries` are the other distributor's part of
    this location's spending and how many invoices it sent, when it already
    delivers.

    Written to be acted on: the action is a few words that fit a table cell,
    the headline one plain sentence, and each point one fact or one thing to
    ask, shortest first."""
    current, alt, saving = standing.current, offer.distributor_name, offer.annual_saving
    large = standing.product_share is not None and standing.product_share >= LARGE_SHARE
    spending = (
        f"{current} gets {_share(standing.share_of_spending)} of your spending. "
        if standing.share_of_spending is not None
        else ""
    ) + "Buying mostly from one distributor usually earns better prices."
    this_product = (
        f"This product is about {usd(standing.product_a_year)} a year, "
        f"{_share(standing.product_share)} of your order with {current}."
        if standing.product_share is not None and standing.product_a_year is not None
        else None
    )

    if offer.yours:
        often = f": {deliveries} invoice{'s' if deliveries != 1 else ''} in the last {LOOKBACK_DAYS} days." if deliveries else "."
        points = [
            f"No new account needed. {alt} already delivers to you{often}",
            spending + (f" {alt} gets {_share(their_share)}." if their_share is not None else ""),
        ]
        if this_product:
            points.append(
                this_product
                + (
                    " That's big enough that moving it could raise your other prices."
                    if large
                    else " That's small enough to move safely."
                )
            )
        points.append(f"Have a rebate or volume deal with {current}? Check this won't drop you below it.")
        if saving is None:
            return Advice(
                "negotiate",
                "Compare on your next order",
                f"You already buy from {alt}. Compare the two prices on your next order.",
                points,
            )
        if saving < WORTH_MOVING:
            return Advice(
                "stay", f"Stay with {current}", f"Too small to switch for. Mention the price to your {current} rep.", points
            )
        if large:
            return Advice(
                "negotiate",
                f"Ask {current} to match",
                f"Ask {current} to match this price before you move it. "
                f"It's {_share(standing.product_share)} of your order with them.",
                points,
            )
        points.append(f"Or ask {current} to match first. It costs nothing.")
        return Advice("move", f"Move it to {alt}", f"Move this to {alt}. They already deliver to you.", points)

    points = [
        f"You'd need a new account with {alt}: usually a credit application and a minimum order.",
        spending + " Ask what splitting your order would change.",
    ]
    if this_product:
        points.append(this_product)
    if flagged.products > 1:
        points.append(
            f"{flagged.products} of your flagged products cost less at {alt}: about {usd(flagged.saving)} a year together."
        )
    if flagged.spent > 0:
        # What an order from them would come to, against a minimum this
        # can't see: the cost a single cheaper product most often runs into.
        several = flagged.products > 1
        points.append(
            f"On {'their' if several else 'its'} own {'these are' if several else 'this is'} about "
            f"{usd(flagged.spent / WEEKS_PER_YEAR)} a week. Ask {alt} for their minimum order: "
            "you may need to move more to buy from them."
        )
    points.append(f"The price is what {offer.distinct_account_count} other businesses pay {alt}. It isn't a quote for you.")
    if saving is None:
        return Advice(
            "stay", f"Stay with {current}", f"Show this price to your {current} rep before looking at a new supplier.", points
        )
    if flagged.saving < WORTH_A_NEW_SUPPLIER:
        return Advice(
            "stay", f"Stay with {current}", f"Not worth a new supplier. Show this price to your {current} rep.", points
        )
    return Advice(
        "negotiate",
        f"Ask {current} to match",
        f"Ask {current} to match this price. If they won't, get a quote from {alt}.",
        points,
    )


def weighed_alternatives(
    db: Session, tenant: Tenant, alerts: Sequence[PriceAlert], exclude_account_key: uuid.UUID
) -> dict[uuid.UUID, list[Alternative]]:
    """find_alternatives, with each one's yearly saving and advice filled in.

    Two more queries for the page, both of this location's own rows: what it
    bought (for quantities) and its invoices (for who it spends with).
    """
    found = find_alternatives(db, tenant, alerts, exclude_account_key)
    alerts = [alert for alert in alerts if alert.id in found]
    if not alerts:
        return found
    bind_tenant(db, tenant.id)
    window = timedelta(days=LOOKBACK_DAYS)
    earliest = min(alert.window_end for alert in alerts) - window
    latest = max(alert.window_end for alert in alerts)
    bought = db.execute(
        select(
            PriceObservation.canonical_sku_id,
            PriceObservation.distributor_id,
            PriceObservation.observed_on,
            PriceObservation.unit_price_base,
            InvoiceLineItem.normalized_qty_base,
        )
        .join(InvoiceLineItem, InvoiceLineItem.id == PriceObservation.invoice_line_item_id)
        .where(
            PriceObservation.tenant_id == tenant.id,
            PriceObservation.observed_on >= earliest,
            PriceObservation.observed_on <= latest,
        )
    ).all()
    # Invoices whose numbers are trusted, as Spending counts them.
    invoices = db.execute(
        select(Invoice.distributor_id, Invoice.invoice_date, Invoice.total).where(
            Invoice.tenant_id == tenant.id,
            Invoice.status.in_([InvoiceStatus.extracted, InvoiceStatus.confirmed]),
            *not_held(),
            Invoice.total.is_not(None),
            Invoice.invoice_date >= earliest,
            Invoice.invoice_date <= latest,
        )
    ).all()
    names = dict(
        db.execute(
            select(Distributor.id, Distributor.name).where(Distributor.id.in_({alert.distributor_id for alert in alerts}))
        ).all()
    )

    standings: dict[uuid.UUID, _Standing] = {}
    spent_with: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    deliveries: dict[uuid.UUID, dict[uuid.UUID, int]] = {}
    for alert in alerts:
        start = alert.window_end - window
        mine = [row for row in bought if start <= row.observed_on <= alert.window_end]
        # A year at the rate of the history actually there, as on Savings.
        factor = annualization([row.observed_on for row in mine], alert.window_end)[1] if mine else Decimal(0)
        here = [
            row
            for row in mine
            if row.canonical_sku_id == alert.canonical_sku_id and row.distributor_id == alert.distributor_id
        ]
        quantity = sum((row.normalized_qty_base or Decimal(0) for row in here), Decimal(0))
        product_spend = sum(((row.normalized_qty_base or Decimal(0)) * row.unit_price_base for row in here), Decimal(0))

        spent: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
        sent: dict[uuid.UUID, int] = defaultdict(int)
        for distributor_id, invoice_date, total in invoices:
            if start <= invoice_date <= alert.window_end:
                spent[distributor_id] += total
                sent[distributor_id] += total > 0  # a credit memo isn't a delivery
        spent_with[alert.id], deliveries[alert.id] = spent, sent
        everything, with_current = sum(spent.values(), Decimal(0)), spent[alert.distributor_id]
        standings[alert.id] = _Standing(
            current=names.get(alert.distributor_id, "your distributor"),
            share_of_spending=with_current / everything if everything > 0 and with_current > 0 else None,
            product_share=min(product_spend / with_current, Decimal(1)) if with_current > 0 and product_spend > 0 else None,
            product_a_year=(product_spend * factor).quantize(_CENT) if product_spend > 0 else None,
        )
        for offer in found[alert.id]:
            if quantity > 0:
                offer.annual_saving = ((alert.current_price - offer.price) * quantity * factor).quantize(_CENT)

    # A new supplier is judged on everything flagged that costs less there:
    # nobody opens an account for one product, and three may be worth it.
    flagged: dict[uuid.UUID, _Flagged] = defaultdict(_Flagged)
    for alert in alerts:
        for offer in found[alert.id]:
            if not offer.yours:
                there = flagged[offer.distributor_id]
                there.products += 1
                there.saving += offer.annual_saving or Decimal(0)
                there.spent += standings[alert.id].product_a_year or Decimal(0)

    for alert in alerts:
        everything = sum(spent_with[alert.id].values(), Decimal(0))
        for offer in found[alert.id]:
            theirs = spent_with[alert.id][offer.distributor_id]
            offer.advice = _advise(
                offer,
                standings[alert.id],
                their_share=theirs / everything if everything > 0 and theirs > 0 else None,
                deliveries=deliveries[alert.id][offer.distributor_id],
                flagged=flagged[offer.distributor_id],
            )
    return found
