"""SPEC.md §9: Insights — open alerts, price history sparkline per SKU,
benchmark position. Read-only: creep alerts are refreshed by
app/api/review.py right after a new price observation actually lands
(a confirm/correct), not on every page view here — an earlier version
called upsert_creep_alerts on every GET, turning a nominally safe/cacheable
read into a required write (and a full per-tenant recompute) on every page
load, refresh, or browser prefetch, a code-review finding on Phase 5.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.benchmark import compute_benchmark
from app.api.deps import get_tenant_or_404
from app.db import get_db_for_tenant
from app.models import CanonicalSku, PriceAlert, PriceObservation
from app.models.enums import AlertStatus
from app.schemas.insights import BenchmarkPosition, InsightCard, PriceHistoryPoint

router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("", response_model=list[InsightCard])
def get_insights(tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)) -> list[InsightCard]:
    tenant = get_tenant_or_404(db, tenant_id)

    alerts = list(
        db.scalars(
            select(PriceAlert)
            .where(PriceAlert.tenant_id == tenant_id, PriceAlert.status == AlertStatus.open)
            .order_by(PriceAlert.created_at.desc())
        )
    )

    # One query for every open alert's price history, not one per alert —
    # grouped in Python by canonical_sku_id below.
    sku_ids = [alert.canonical_sku_id for alert in alerts]
    history_by_sku: dict[uuid.UUID, list[tuple]] = {}
    if sku_ids:
        for sku_id, observed_on, price in db.execute(
            select(PriceObservation.canonical_sku_id, PriceObservation.observed_on, PriceObservation.unit_price_base)
            .where(PriceObservation.tenant_id == tenant_id, PriceObservation.canonical_sku_id.in_(sku_ids))
            .order_by(PriceObservation.observed_on)
        ).all():
            history_by_sku.setdefault(sku_id, []).append((observed_on, price))

    cards = []
    for alert in alerts:
        sku = db.get(CanonicalSku, alert.canonical_sku_id)
        history_rows = history_by_sku.get(alert.canonical_sku_id, [])

        # Uses the alert's own window_end as "as of," not wall-clock today —
        # keeps the benchmark comparison anchored to the same period the
        # alert itself covers (detect_price_creep has no as_of concept of its
        # own; it windows by observation count, see price_creep.py).
        benchmark = compute_benchmark(
            db, alert.canonical_sku_id, tenant.metro, alert.window_end, exclude_tenant_id=tenant_id
        )

        cards.append(
            InsightCard(
                alert_id=alert.id,
                canonical_sku_id=alert.canonical_sku_id,
                canonical_sku_name=sku.name if sku else "(unknown SKU)",
                alert_type=alert.alert_type.value,
                baseline_price=alert.baseline_price,
                current_price=alert.current_price,
                pct_change=alert.pct_change,
                window_start=alert.window_start,
                window_end=alert.window_end,
                status=alert.status.value,
                price_history=[
                    PriceHistoryPoint(observed_on=observed_on, unit_price_base=price)
                    for observed_on, price in history_rows
                ],
                benchmark=(
                    BenchmarkPosition(
                        p25=benchmark.p25,
                        p50=benchmark.p50,
                        p75=benchmark.p75,
                        tenant_price=alert.current_price,
                        distinct_tenant_count=benchmark.distinct_tenant_count,
                        scope=benchmark.scope,
                    )
                    if benchmark is not None
                    else None
                ),
            )
        )
    return cards
