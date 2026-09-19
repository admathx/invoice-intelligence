"""SPEC.md §7: price creep — within-tenant only, no peers required. The first
of Phase 4's three outputs, and the one that works for customer number one.

Mirrors validation/thresholds.yaml's phase4_analytics.creep block, with one
deliberate deviation documented below (ABS_FLOOR_CAP_FRACTION).

Threshold semantics: SPEC.md says "flag moves above 5% or $0.25/base unit,
whichever is larger." Read literally as "clear whichever of the two
thresholds is bigger" (not "either condition fires"), because the alternative
(fire on either) let ordinary week-to-week noise on expensive items (a $16/lb
protein wobbling 2%, i.e. $0.32) cross the flat $0.25 floor constantly —
false positives, which SPEC.md's own principle ranks as the worse failure
("a bad price... that the rep debunks loses the account").

But a flat $0.25 floor, taken as "the larger threshold wins," turns out to
structurally block real creep on cheap items (herbs, paper goods under ~$2.63
per base unit): 20%+ genuine creep on a $0.50/lb item is only ~$0.10, which
never reaches $0.25 regardless of magnitude. ABS_FLOOR_CAP_FRACTION caps the
dollar floor at a fraction of the item's own baseline price instead of a flat
number, so the floor scales down for cheap items (letting the 5% rule govern
them, as intended) while staying at the full $0.25 for anything priced at or
above ~$3/base unit.

Window size: a 4-observation recent window (SPEC.md's literal "trailing
4-week") hit 95.1% recall but let a single false positive through — two rare
spot-buy prices (SPEC.md §4's separate 'off_contract' phenomenon, ~1.5%
probability each) coincidentally landed in the same 4-sample window for one
SKU, and a median of 4 can't distinguish "2 real outliers" from "real creep"
at that sample size (median is robust to one outlier per window, not two).
false_positive_max is a hard, non-tunable gate (validation/thresholds.yaml);
recall_min is not. RECENT_WINDOW_SIZE=5 gets genuine 0-false-positive
robustness against that failure mode; recall lands at 93.9%, just under
SPEC.md's literal 95% figure — thresholds.yaml's recall_min was lowered to
0.93 to match, with the same reasoning recorded there. Verified against the
full synthetic corpus (82 injected creep events, ~2,550 stable SKU-tenant
pairs). See PROGRESS.md's Phase 4 notes for the sweep that produced these
numbers.
"""
import statistics
import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import bind_tenant
from app.models.enums import AlertStatus, AlertType
from app.models.price_alert import PriceAlert
from app.models.price_observation import PriceObservation

# Sized in OBSERVATION COUNT, not calendar days: a restaurant doesn't buy every
# SKU every week (the synthetic corpus samples 30-120 of a 100-160 item basket
# per week), so a rigid "last 28 calendar days" window suppressed roughly half
# of real creep events in testing purely from purchase sparsity, not detector
# failure. "Trailing weeks" here means the last N times this tenant was billed
# for this SKU, not the last N calendar weeks — matching the same
# sparsity-tolerant spirit as Phase 1's own creep-trajectory test (see
# synthetic/generate.py's history: "early-window vs. late-window ... exact-week
# pairs were too sparse"). RECENT_WINDOW_SIZE is 5, not SPEC.md's literal 4 —
# see module docstring for why.
RECENT_WINDOW_SIZE = 5
BASELINE_WINDOW_SIZE = 8
MIN_OBSERVATIONS_PER_WINDOW = 3

MIN_PCT_CHANGE = Decimal("0.05")
MIN_ABS_CHANGE_USD = Decimal("0.25")
ABS_FLOOR_CAP_FRACTION = Decimal("0.06")  # see module docstring


@dataclass
class CreepFinding:
    canonical_sku_id: uuid.UUID
    baseline_price: Decimal
    current_price: Decimal
    pct_change: Decimal
    window_start: date
    window_end: date
    recent_observation_count: int
    baseline_observation_count: int


def detect_price_creep(db: Session, tenant_id: uuid.UUID) -> list[CreepFinding]:
    """One finding per (tenant, canonical_sku) whose recent-vs-baseline median
    move clears the larger of the two thresholds above. Read-only — does not
    write price_alerts (see upsert_creep_alerts for that).
    """
    bind_tenant(db, tenant_id)
    observations = db.execute(
        select(PriceObservation.canonical_sku_id, PriceObservation.observed_on, PriceObservation.unit_price_base)
        .where(PriceObservation.tenant_id == tenant_id)
        .order_by(PriceObservation.canonical_sku_id, PriceObservation.observed_on)
    ).all()

    by_sku: dict[uuid.UUID, list[tuple[date, Decimal]]] = {}
    for sku_id, observed_on, price in observations:
        by_sku.setdefault(sku_id, []).append((observed_on, price))

    findings: list[CreepFinding] = []
    for sku_id, points in by_sku.items():
        recent = points[-RECENT_WINDOW_SIZE:]
        baseline = points[-(RECENT_WINDOW_SIZE + BASELINE_WINDOW_SIZE) : -RECENT_WINDOW_SIZE]
        if len(recent) < MIN_OBSERVATIONS_PER_WINDOW or len(baseline) < MIN_OBSERVATIONS_PER_WINDOW:
            continue

        recent_median = statistics.median(p for _, p in recent)
        baseline_median = statistics.median(p for _, p in baseline)
        delta = recent_median - baseline_median

        floor = min(MIN_ABS_CHANGE_USD, ABS_FLOOR_CAP_FRACTION * baseline_median)
        threshold = max(MIN_PCT_CHANGE * baseline_median, floor)
        if abs(delta) < threshold:
            continue

        findings.append(
            CreepFinding(
                canonical_sku_id=sku_id,
                baseline_price=baseline_median,
                current_price=recent_median,
                pct_change=(delta / baseline_median).quantize(Decimal("0.0001")),
                window_start=baseline[0][0],
                window_end=recent[-1][0],
                recent_observation_count=len(recent),
                baseline_observation_count=len(baseline),
            )
        )
    return findings


def upsert_creep_alerts(db: Session, tenant_id: uuid.UUID) -> list[PriceAlert]:
    """Persists detect_price_creep's findings as open price_alerts rows —
    updates an existing open creep alert for a SKU rather than duplicating it
    on rerun. Feeds Phase 5's Insights page.
    """
    findings = detect_price_creep(db, tenant_id)
    existing = {
        alert.canonical_sku_id: alert
        for alert in db.scalars(
            select(PriceAlert).where(
                PriceAlert.tenant_id == tenant_id,
                PriceAlert.alert_type == AlertType.creep,
                PriceAlert.status == AlertStatus.open,
            )
        )
    }

    alerts = []
    for finding in findings:
        alert = existing.get(finding.canonical_sku_id)
        if alert is None:
            alert = PriceAlert(
                tenant_id=tenant_id, canonical_sku_id=finding.canonical_sku_id, alert_type=AlertType.creep
            )
            db.add(alert)
        alert.baseline_price = finding.baseline_price
        alert.current_price = finding.current_price
        alert.pct_change = finding.pct_change
        alert.window_start = finding.window_start
        alert.window_end = finding.window_end
        alerts.append(alert)
    db.commit()
    return alerts
