"""What a location's buying costs in a year, and what would change it.

Spending says what was spent, month by month. Price alerts and Savings each
say one thing about one product. None of them answers the question an owner
plans with: what does a year of this cost at today's prices, and how far
would it move if prices went up again, if the rep gave way, if we bought
less? This builds the numbers that question is asked of.

The year is the last LOOKBACK_DAYS of the location's own invoices, counted
back from its latest one, scaled up the way Savings scales (never from less
history than MIN_ANNUALIZATION_DAYS). Each product is what was bought of it
from one distributor, at what that distributor charges now: "now" as a
price alert means it, the middle of the last few prices.

Only products with a price per unit are in it: items not yet matched, and
fees, have no price to move. `coverage` says how much of the location's
spending that is, so a page can say what its total leaves out.

The scenarios are things that could happen to the prices, each measured
against that year. They overlap (a flagged product is often on Savings
too), so each is its own answer and they are not to be added up:

- increases_reversed: every open price alert's price goes back to what it
  was before it rose;
- cheaper_elsewhere: every flagged product is bought at the cheapest price
  found at another distributor, by a match or by moving
  (app/analytics/switching.py);
- savings_targets: every target on Savings is met (as that page has them
  today, which is where the scenario links);
- rise_again: every price moves again as it did over the window.

Anything else (a category up 8%, volume down 10%) is arithmetic on the
products themselves, which the page does as the person moves a slider.
"""
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.benchmark import LOOKBACK_DAYS, account_key_for, own_price
from app.analytics.negotiation import NegotiationBasis, annualization, build_negotiation_sheet
from app.analytics.price_creep import RECENT_WINDOW_SIZE
from app.analytics.switching import alternatives_or_none
from app.db import bind_tenant
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation, Tenant
from app.models.enums import AlertStatus, InvoiceStatus
from app.models.invoice import not_held

_CENT = Decimal("0.01")
_PCT_PLACES = Decimal("0.0001")


@dataclass
class ProductCost:
    """One product from one distributor, over a year."""

    canonical_sku_id: uuid.UUID
    name: str
    category: str
    distributor: str
    yearly_cost: Decimal
    # How its price moved over the window, 0.05 for 5% up; None when it was
    # bought once, which shows no movement either way.
    recent_change: Decimal | None


@dataclass
class Scenario:
    key: str
    # What the year would cost more (or, negative, less) if it happened.
    yearly_change: Decimal
    # How many products it touches.
    products: int


@dataclass
class Costs:
    # The window the year is projected from; None when there's no history.
    window_start: date | None = None
    window_end: date | None = None
    # Days of that window there are invoices for (<= LOOKBACK_DAYS).
    window_days: int = 0
    yearly_cost: Decimal = Decimal("0.00")
    # Of what was spent in the window, the part these products are, 0..1.
    coverage: Decimal | None = None
    products: list[ProductCost] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)


def _moved(prices: list[Decimal]) -> Decimal | None:
    """How a price moved from the start of a run of purchases to its end:
    the middle of the last few against the middle of the first few, so one
    spot buy at either end isn't the answer."""
    if len(prices) < 2:
        return None
    few = max(1, min(RECENT_WINDOW_SIZE, len(prices) // 2))
    first, last = own_price(prices[:few]), own_price(prices[-few:])
    return (last / first - 1).quantize(_PCT_PLACES) if first > 0 else None


def build_costs(db: Session, tenant: Tenant, today: date) -> Costs:
    bind_tenant(db, tenant.id)
    latest = db.scalar(
        select(func.max(PriceObservation.observed_on)).where(
            PriceObservation.tenant_id == tenant.id, PriceObservation.observed_on <= today
        )
    )
    if latest is None:
        return Costs()
    start = latest - timedelta(days=LOOKBACK_DAYS)
    rows = db.execute(
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
            PriceObservation.observed_on >= start,
            PriceObservation.observed_on <= latest,
        )
        .order_by(PriceObservation.observed_on, PriceObservation.id)
    ).all()
    window_days, factor = annualization([row.observed_on for row in rows], latest)

    bought: dict[tuple[uuid.UUID, uuid.UUID], list] = {}
    for row in rows:
        bought.setdefault((row.canonical_sku_id, row.distributor_id), []).append(row)
    skus = {
        sku.id: sku
        for sku in db.scalars(select(CanonicalSku).where(CanonicalSku.id.in_({sku_id for sku_id, _ in bought})))
    }
    distributors = dict(
        db.execute(select(Distributor.id, Distributor.name).where(Distributor.id.in_({d for _, d in bought}))).all()
    )

    products: list[ProductCost] = []
    yearly_quantity: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    spent_on_products = Decimal(0)
    for key, purchases in bought.items():
        quantity = sum((row.normalized_qty_base or Decimal(0) for row in purchases), Decimal(0))
        spent_on_products += sum(
            ((row.normalized_qty_base or Decimal(0)) * row.unit_price_base for row in purchases), Decimal(0)
        )
        if quantity <= 0:
            continue
        prices = [row.unit_price_base for row in purchases]
        yearly_quantity[key] = quantity * factor
        sku = skus.get(key[0])
        products.append(
            ProductCost(
                canonical_sku_id=key[0],
                name=sku.name if sku else "(unknown product)",
                category=sku.category if sku else "other",
                distributor=distributors.get(key[1], "Unknown distributor"),
                yearly_cost=(yearly_quantity[key] * own_price(prices[-RECENT_WINDOW_SIZE:])).quantize(_CENT),
                recent_change=_moved(prices),
            )
        )
    products.sort(key=lambda p: (-p.yearly_cost, p.name))

    # Everything on the window's trusted invoices, items before tax, as
    # Spending counts it: what the products above are a part of.
    spent = db.scalar(
        select(func.coalesce(func.sum(InvoiceLineItem.extended_price), 0))
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(
            Invoice.tenant_id == tenant.id,
            Invoice.status.in_([InvoiceStatus.extracted, InvoiceStatus.confirmed]),
            *not_held(),
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= latest,
        )
    )
    costs = Costs(
        window_start=start,
        window_end=latest,
        window_days=min(window_days, LOOKBACK_DAYS),
        yearly_cost=sum((p.yearly_cost for p in products), Decimal(0)).quantize(_CENT),
        coverage=min(spent_on_products / spent, Decimal(1)).quantize(_PCT_PLACES) if spent and spent > 0 else None,
        products=products,
    )

    alerts = [
        alert
        for alert in db.scalars(
            select(PriceAlert).where(
                PriceAlert.tenant_id == tenant.id,
                PriceAlert.status == AlertStatus.open,
                PriceAlert.distributor_id.is_not(None),
            )
        )
        # One about a product no longer bought has nothing to move.
        if (alert.canonical_sku_id, alert.distributor_id) in yearly_quantity
    ]
    reversed_ = sum(
        (
            (alert.current_price - alert.baseline_price) * yearly_quantity[(alert.canonical_sku_id, alert.distributor_id)]
            for alert in alerts
        ),
        Decimal(0),
    )
    cheapest = [
        offers[0].annual_saving
        for offers in alternatives_or_none(db, tenant, alerts, account_key_for(db, tenant.id)).values()
        if offers[0].annual_saving
    ]
    # As of today, as the Savings page builds it: the scenario says "every
    # target on Savings" and links there, so it is that page's total.
    sheet = build_negotiation_sheet(db, tenant.id, today, NegotiationBasis.auto)
    moving = [p for p in products if p.recent_change]
    costs.scenarios = [
        Scenario("increases_reversed", -reversed_.quantize(_CENT), len(alerts)),
        Scenario("cheaper_elsewhere", -sum(cheapest, Decimal(0)).quantize(_CENT), len(cheapest)),
        Scenario("savings_targets", -sheet.total_annualized_savings.quantize(_CENT), len(sheet.lines)),
        Scenario(
            "rise_again", sum((p.yearly_cost * p.recent_change for p in moving), Decimal(0)).quantize(_CENT), len(moving)
        ),
    ]
    return costs
