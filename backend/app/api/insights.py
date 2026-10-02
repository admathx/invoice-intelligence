"""SPEC.md §9: Insights — open alerts, price history sparkline per SKU,
benchmark position; and dismissing an alert someone has dealt with.
Viewing is read-only: creep alerts are refreshed by
app/api/review.py right after a new price observation actually lands
(a confirm/correct), not on every page view here — an earlier version
called upsert_creep_alerts on every GET, turning a nominally safe/cacheable
read into a required write (and a full per-tenant recompute) on every page
load, refresh, or browser prefetch, a code-review finding on Phase 5.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.analytics.alternatives import find_alternatives
from app.analytics.benchmark import account_key_for, compute_benchmark
from app.api.deps import get_tenant_or_404
from app.auth import current_user, get_db_for_tenant
from app.models import CanonicalSku, Distributor, PriceAlert, PriceObservation, User
from app.models.enums import AlertStatus
from app.schemas.insights import AlternativeOut, BenchmarkPosition, InsightCard, PriceHistoryPoint

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
    # grouped in Python by (canonical_sku_id, distributor) below. An alert is
    # about one distributor's price, so its chart shows that distributor's.
    sku_ids = [alert.canonical_sku_id for alert in alerts]
    history: dict[tuple[uuid.UUID, uuid.UUID], list[tuple]] = {}
    if sku_ids:
        for sku_id, distributor_id, observed_on, price in db.execute(
            select(
                PriceObservation.canonical_sku_id,
                PriceObservation.distributor_id,
                PriceObservation.observed_on,
                PriceObservation.unit_price_base,
            )
            .where(PriceObservation.tenant_id == tenant_id, PriceObservation.canonical_sku_id.in_(sku_ids))
            .order_by(PriceObservation.observed_on)
        ).all():
            history.setdefault((sku_id, distributor_id), []).append((observed_on, price))

    def history_for(alert: PriceAlert) -> list[tuple]:
        if alert.distributor_id is not None:
            return history.get((alert.canonical_sku_id, alert.distributor_id), [])
        # From before alerts had a distributor: every distributor's, as then.
        return sorted(row for (sku_id, _), rows in history.items() if sku_id == alert.canonical_sku_id for row in rows)

    # Resolved once, outside the loop: the asking tenant's business is the
    # same for every alert on the page.
    exclude_account_key = account_key_for(db, tenant_id)

    # One query for every SKU name on the page, not one db.get() per alert —
    # the same rule app/api/negotiation.py already follows for its sheet.
    names = {sku.id: sku.name for sku in db.scalars(select(CanonicalSku).where(CanonicalSku.id.in_(sku_ids)))}
    distributor_ids = {alert.distributor_id for alert in alerts if alert.distributor_id is not None}
    distributors = dict(db.execute(select(Distributor.id, Distributor.name).where(Distributor.id.in_(distributor_ids))).all())

    alternatives = find_alternatives(db, tenant, alerts, exclude_account_key)

    cards = []
    for alert in alerts:
        history_rows = history_for(alert)

        # Uses the alert's own window_end as "as of," not wall-clock today —
        # keeps the benchmark comparison anchored to the same period the
        # alert itself covers (detect_price_creep has no as_of concept of its
        # own; it windows by observation count, see price_creep.py).
        # subject_price is the alert's own current price, so the returned
        # percentile answers "where does what I'm paying now sit among peers".
        benchmark = compute_benchmark(
            db,
            alert.canonical_sku_id,
            tenant.metro,
            alert.window_end,
            exclude_account_key=exclude_account_key,
            subject_price=alert.current_price,
        )

        cards.append(
            InsightCard(
                alert_id=alert.id,
                canonical_sku_id=alert.canonical_sku_id,
                canonical_sku_name=names.get(alert.canonical_sku_id, "(unknown SKU)"),
                distributor_name=distributors.get(alert.distributor_id),
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
                # Both conditions, not just the first: the spectrum is built
                # around the percentile, so a cell without one has no chart to
                # draw. subject_percentile is always set here today (a
                # subject_price is passed above and alerts carry a non-null
                # current_price), but failing closed beats a required schema
                # field turning an insights page into a 500 if that changes.
                benchmark=(
                    BenchmarkPosition(
                        p25=benchmark.p25,
                        p50=benchmark.p50,
                        p75=benchmark.p75,
                        tenant_price=alert.current_price,
                        percentile=benchmark.subject_percentile,
                        distinct_account_count=benchmark.distinct_account_count,
                        scope=benchmark.scope,
                    )
                    if benchmark is not None and benchmark.subject_percentile is not None
                    else None
                ),
                alternatives=[AlternativeOut.model_validate(offer) for offer in alternatives.get(alert.id, [])],
            )
        )
    return cards


@router.post("/{alert_id}/dismiss", status_code=204)
def dismiss_alert(
    alert_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> None:
    """Take an increase off Price alerts once it's been dealt with (the rep
    was called, the price accepted). It stays off unless the price climbs
    clearly further (app/analytics/price_creep.py REOPEN_ABOVE_DISMISSED)."""
    get_tenant_or_404(db, tenant_id)
    alert = db.get(PriceAlert, alert_id)  # tenant-scoped: another location's is simply not found
    if alert is None or alert.status != AlertStatus.open:
        raise HTTPException(status_code=404, detail="That alert isn't open any more.")
    alert.status = AlertStatus.dismissed
    product = db.scalar(select(CanonicalSku.name).where(CanonicalSku.id == alert.canonical_sku_id))
    distributor = db.scalar(select(Distributor.name).where(Distributor.id == alert.distributor_id))
    audit.record(
        db,
        user,
        "price_alert.dismissed",
        "price_alert",
        alert.id,
        tenant_id,
        product=product,
        distributor=distributor,
        pct_change=alert.pct_change,
        current_price=alert.current_price,
    )
    db.commit()
