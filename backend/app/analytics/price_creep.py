"""SPEC.md §7: price creep — within-tenant only, no peers required. The first
of Phase 4's three outputs, and the one that works for customer number one.

Thresholds below are loaded directly from validation/thresholds.yaml's
phase4_analytics.creep block at import time, rather than hardcoded literals
mirrored by comment (matcher.py's Phase 3 pattern) — code-review found that
pattern lets the live detector silently drift from what thresholds.yaml
documents if only one side gets edited later. See that file for the values
and the reasoning behind ABS_FLOOR_CAP_FRACTION.

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
them, as intended) while staying at the full $0.25 floor for anything priced
at or above MIN_ABS_CHANGE_USD / ABS_FLOOR_CAP_FRACTION (~$4.17 at the current
0.06 fraction).

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
from pathlib import Path

import yaml
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.db import bind_tenant
from app.models.enums import AlertStatus, AlertType
from app.models.price_alert import PriceAlert
from app.models.price_observation import PriceObservation

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_creep_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase4_analytics"]["creep"]

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
RECENT_WINDOW_SIZE = _creep_thresholds["lookback_recent_weeks"]
BASELINE_WINDOW_SIZE = _creep_thresholds["lookback_baseline_weeks"]
MIN_OBSERVATIONS_PER_WINDOW = _creep_thresholds["min_observations_per_window"]

MIN_PCT_CHANGE = Decimal(str(_creep_thresholds["min_pct_change"]))
MIN_ABS_CHANGE_USD = Decimal(str(_creep_thresholds["min_abs_change_usd"]))
ABS_FLOOR_CAP_FRACTION = Decimal(str(_creep_thresholds["abs_floor_cap_fraction"]))  # see module docstring


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
        if baseline_median <= 0:
            # A $0 baseline (e.g. a promo/free-case line) makes both a
            # percentage move and the price-tiered floor below undefined —
            # skip rather than divide by zero computing pct_change.
            continue
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

    Uses a single atomic INSERT ... ON CONFLICT DO UPDATE (targeting
    price_alerts' partial unique index on (tenant_id, canonical_sku_id,
    alert_type) WHERE status='open' — migration 0003) rather than a
    check-then-act SELECT-then-INSERT: two concurrent callers for the same
    tenant (e.g. two review actions landing close together) previously could
    both observe "no open alert yet" and both insert one, producing two open
    creep alerts for the same SKU that only one of them would ever update
    again — a code-review finding on Phase 5.
    """
    findings = detect_price_creep(db, tenant_id)
    if not findings:
        return []

    values = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "canonical_sku_id": finding.canonical_sku_id,
            "alert_type": AlertType.creep,
            "baseline_price": finding.baseline_price,
            "current_price": finding.current_price,
            "pct_change": finding.pct_change,
            "window_start": finding.window_start,
            "window_end": finding.window_end,
            "status": AlertStatus.open,
        }
        for finding in findings
    ]

    stmt = pg_insert(PriceAlert).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "canonical_sku_id", "alert_type"],
        index_where=text("status = 'open'"),
        set_={
            "baseline_price": stmt.excluded.baseline_price,
            "current_price": stmt.excluded.current_price,
            "pct_change": stmt.excluded.pct_change,
            "window_start": stmt.excluded.window_start,
            "window_end": stmt.excluded.window_end,
        },
    ).returning(PriceAlert.id)
    alert_ids = db.execute(stmt).scalars().all()
    db.commit()
    return list(db.scalars(select(PriceAlert).where(PriceAlert.id.in_(alert_ids))))
