"""SPEC.md §7: negotiation sheet — top 15 SKUs ranked by dollars recoverable,
not percentage gap ("a 3% gap on cheese beats a 40% gap on toothpicks").

Every line traces back to specific invoice_line_item_id rows (SPEC.md Phase 4
exit criteria): current_price comes from exactly one observation, and
quantity is the sum over an explicit, listed set of line items.
"""
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.benchmark import compute_benchmark
from app.db import bind_tenant
from app.models.invoice_line_item import InvoiceLineItem
from app.models.price_observation import PriceObservation
from app.models.tenant import Tenant

TOP_N = 15
LOOKBACK_DAYS = 90
ANNUALIZATION_FACTOR = Decimal(4)  # a 90-day window is ~1 quarter of a year


@dataclass
class NegotiationLine:
    canonical_sku_id: uuid.UUID
    current_price: Decimal
    current_price_line_item_id: uuid.UUID
    target_price: Decimal
    peer_distinct_tenant_count: int
    trailing_90d_quantity: Decimal
    quantity_line_item_ids: list[uuid.UUID]
    recoverable_90d: Decimal
    annualized_savings: Decimal


def build_negotiation_sheet(db: Session, tenant_id: uuid.UUID, as_of: date) -> list[NegotiationLine]:
    bind_tenant(db, tenant_id)
    tenant = db.get(Tenant, tenant_id)
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)

    rows = db.execute(
        select(
            PriceObservation.canonical_sku_id,
            PriceObservation.observed_on,
            PriceObservation.unit_price_base,
            PriceObservation.invoice_line_item_id,
            InvoiceLineItem.normalized_qty_base,
        )
        .join(InvoiceLineItem, InvoiceLineItem.id == PriceObservation.invoice_line_item_id)
        .where(
            PriceObservation.tenant_id == tenant_id,
            PriceObservation.observed_on >= window_start,
            PriceObservation.observed_on <= as_of,
        )
        # Tertiary sort key: observed_on is a Date, so two deliveries of the
        # same SKU from two distributors on the same calendar date would
        # otherwise tie with no deterministic winner for "current price".
        .order_by(PriceObservation.canonical_sku_id, PriceObservation.observed_on.desc(), PriceObservation.id.desc())
    ).all()

    by_sku: dict[uuid.UUID, list[tuple[date, Decimal, uuid.UUID, Decimal]]] = {}
    for sku_id, observed_on, price, line_item_id, qty in rows:
        by_sku.setdefault(sku_id, []).append((observed_on, price, line_item_id, qty))

    candidates: list[NegotiationLine] = []
    for sku_id, points in by_sku.items():
        benchmark = compute_benchmark(db, sku_id, tenant.metro, as_of, volume_tier=None, exclude_tenant_id=tenant_id)
        if benchmark is None:
            continue

        current_price = points[0][1]  # points is sorted newest-first
        current_price_line_item_id = points[0][2]
        if current_price <= benchmark.p25:
            continue  # no overpay to recover

        trailing_qty = sum((qty for _, _, _, qty in points), Decimal(0))
        recoverable_90d = ((current_price - benchmark.p25) * trailing_qty).quantize(Decimal("0.0001"))
        candidates.append(
            NegotiationLine(
                canonical_sku_id=sku_id,
                current_price=current_price,
                current_price_line_item_id=current_price_line_item_id,
                target_price=benchmark.p25,
                peer_distinct_tenant_count=benchmark.distinct_tenant_count,
                trailing_90d_quantity=trailing_qty,
                quantity_line_item_ids=[lid for _, _, lid, _ in points],
                recoverable_90d=recoverable_90d,
                annualized_savings=(recoverable_90d * ANNUALIZATION_FACTOR).quantize(Decimal("0.0001")),
            )
        )

    candidates.sort(key=lambda line: line.recoverable_90d, reverse=True)
    return candidates[:TOP_N]
