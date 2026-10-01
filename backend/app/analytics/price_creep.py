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
at or above MIN_ABS_CHANGE_USD / ABS_FLOOR_CAP_FRACTION ($5 at the current
0.05 fraction, the same as MIN_PCT_CHANGE).

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
from sqlalchemy import func, or_, select, text, tuple_, update
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


# How much further a dismissed alert's price has to climb before it's news
# again (see upsert_creep_alerts).
REOPEN_ABOVE_DISMISSED = Decimal("0.05")


@dataclass
class CreepFinding:
    canonical_sku_id: uuid.UUID
    distributor_id: uuid.UUID
    baseline_price: Decimal
    current_price: Decimal
    pct_change: Decimal
    window_start: date
    window_end: date
    recent_observation_count: int
    baseline_observation_count: int


def _highs_it_came_back_from(baseline: list[Decimal], recent: list[Decimal]) -> list[Decimal]:
    """Earlier prices that were followed by a clearly lower one: levels the
    price reached and then left. A price that stepped up and stayed has
    none (it never came back down); neither does ordinary noise, which
    stays inside MIN_PCT_CHANGE."""
    later = baseline[1:] + recent
    return [
        price
        for i, price in enumerate(baseline)
        if any(after < price * (1 - MIN_PCT_CHANGE) for after in later[i:])
    ]


# How many prices before the recent ones are looked through for a level the
# price was cut from and has only come back to (see _back_where_it_was).
RETURN_LOOKBACK = RECENT_WINDOW_SIZE + 2 * BASELINE_WINDOW_SIZE


def _threshold(baseline_price: Decimal) -> Decimal:
    """How far above `baseline_price` counts as an increase: the larger of
    the percentage and the dollar floor (see the module docstring)."""
    floor = min(MIN_ABS_CHANGE_USD, ABS_FLOOR_CAP_FRACTION * baseline_price)
    return max(MIN_PCT_CHANGE * baseline_price, floor)


def _back_where_it_was(earlier: list[Decimal], recent: list[Decimal], current_price: Decimal) -> bool:
    """Whether the price has only returned to a level it held before and
    was cut from. A price cut for a few weeks (a promotion) and then
    restored puts the cheap weeks in the baseline, and the old price coming
    back read as an 11% increase. It is one only once it is clearly above
    what was paid before the cut. Several earlier prices, because one spike
    is not a level; and a price that eased 2% and then climbed 7% was never
    cut, so it is climbing."""
    held = _highs_it_came_back_from(earlier, recent)
    if len(held) < MIN_OBSERVATIONS_PER_WINDOW:
        return False
    level = statistics.median(held)
    return current_price - level < _threshold(level)


# How much stronger than the usual threshold a short history's trend must be
# before it alerts with the medians short of it.
SHORT_HISTORY_TREND_FACTOR = 2


def _trend(prices: list[Decimal]) -> Decimal:
    """How far the price has moved from the first to the last of `prices`,
    going by the typical step between any two of them (the median of every
    pair's slope, which one or two odd prices can't drag)."""
    slopes = [
        (prices[j] - prices[i]) / (j - i) for i in range(len(prices)) for j in range(i + 1, len(prices))
    ]
    return statistics.median(slopes) * (len(prices) - 1)


# How far back a slow rise is measured from, and how much more than the
# usual threshold it has to come to (see _slow_move). Twice: what food costs
# drifts by itself, and on the corpus proteins nobody raised the price of
# were 5% dearer after six months.
SLOW_SPAN_DAYS = 365
SLOW_MOVE_FACTOR = 2


@dataclass
class _Move:
    """An increase in one series of prices: from what, to what, over when."""

    baseline_price: Decimal
    current_price: Decimal
    window_start: date
    window_end: date
    recent_observation_count: int
    baseline_observation_count: int


def _recent_move(points: list[tuple[date, Decimal]]) -> _Move | None:
    """The increase the last few prices show over the ones just before
    them, if they show one. `points` is one product from one distributor,
    oldest first."""
    recent = points[-RECENT_WINDOW_SIZE:]
    baseline = points[-(RECENT_WINDOW_SIZE + BASELINE_WINDOW_SIZE) : -RECENT_WINDOW_SIZE]
    short_history = len(baseline) < MIN_OBSERVATIONS_PER_WINDOW
    if short_history:
        # A short history (six or seven prices): the newest few against
        # the ones before. Per distributor, a second distributor often has
        # this few, and a 12% rise across six US Foods invoices went
        # unflagged for want of eight. Each side still has the minimum.
        recent = points[-MIN_OBSERVATIONS_PER_WINDOW:]
        baseline = points[:-MIN_OBSERVATIONS_PER_WINDOW][-BASELINE_WINDOW_SIZE:]
    if len(recent) < MIN_OBSERVATIONS_PER_WINDOW or len(baseline) < MIN_OBSERVATIONS_PER_WINDOW:
        return None

    recent_prices = [p for _, p in recent]
    baseline_prices = [p for _, p in baseline]
    current_price = statistics.median(recent_prices)
    baseline_price = statistics.median(baseline_prices)
    if baseline_price <= 0:
        # A $0 baseline (e.g. a promo/free-case line) makes both a
        # percentage move and the price-tiered floor below undefined —
        # skip rather than divide by zero computing pct_change.
        return None
    # Not above where the price has already been and come back from:
    # produce that swings a quarter either way from week to week
    # (avocados, limes) isn't creeping when a run of dear weeks lands in
    # the recent window. On a realistic series that opened and closed an
    # alert eight times in fourteen weeks, each one an email. Twice or
    # more, because a single spike says nothing about the usual range;
    # and only levels it came back from, so a price that stepped up and
    # stayed is still an increase for as long as it was.
    left_behind = _highs_it_came_back_from(baseline_prices, recent_prices)
    if len(left_behind) >= 2 and current_price <= max(left_behind):
        return None

    delta = current_price - baseline_price
    threshold = _threshold(baseline_price)
    # Increases only. A price coming down is good news, and every place
    # alerts are shown ("Price alerts", the weekly email, the price-
    # increase email) presents them as increases: a -15% alert read as
    # "▲ -15%" under "price increases". Any open alert on a price that
    # has since fallen back is resolved below, like any other.
    if delta < threshold:
        # On a short history the two medians sit only three prices apart,
        # so a steady climb shows as a fraction of itself: mozzarella up
        # 19% across six invoices measured 4.7%, and went unflagged. The
        # trend across the whole history is looked at instead, and has to
        # be clearly stronger (SHORT_HISTORY_TREND_FACTOR) to count.
        if not short_history or delta <= 0:
            return None
        rise = _trend(baseline_prices + recent_prices)
        if rise < SHORT_HISTORY_TREND_FACTOR * threshold:
            return None

    earlier = [p for _, p in points[: -len(recent)][-RETURN_LOOKBACK:]]
    if _back_where_it_was(earlier, recent_prices, current_price):
        return None

    return _Move(baseline_price, current_price, baseline[0][0], recent[-1][0], len(recent), len(baseline))


def _slow_move(points: list[tuple[date, Decimal]]) -> _Move | None:
    """An increase too gradual for _recent_move to see: a price that goes
    up 0.4% a week is 23% dearer in a year, and no run of thirteen
    purchases ever shows more than 3% of it.

    The last few prices against the oldest of the past year. Measured from
    the last increase that _recent_move would have reported, when there is
    one, so a step that was news in March isn't news again in September."""
    short = RECENT_WINDOW_SIZE + BASELINE_WINDOW_SIZE
    latest = points[-1][0]
    span = [point for point in points if (latest - point[0]).days <= SLOW_SPAN_DAYS]
    earlier = span[:-short]  # older than anything _recent_move compares
    if len(earlier) < MIN_OBSERVATIONS_PER_WINDOW:
        return None
    recent = span[-RECENT_WINDOW_SIZE:]
    start = earlier[:BASELINE_WINDOW_SIZE]
    recent_prices = [p for _, p in recent]
    current_price = statistics.median(recent_prices)
    baseline_price = statistics.median([p for _, p in start])
    first = len(points) - len(span)
    if baseline_price <= 0 or current_price - baseline_price < SLOW_MOVE_FACTOR * _threshold(baseline_price):
        return None
    # As in _recent_move: a level it has been at and come back from, twice
    # or more, is its usual range and not a rise; and a price that has only
    # returned to what it was before a cut hasn't risen. The prices just
    # before the year are looked at too: when the year's oldest prices are
    # a promotion's, the old price coming back would otherwise be news a
    # year after it did.
    earlier = [p for _, p in points[max(0, first - RETURN_LOOKBACK) : -RECENT_WINDOW_SIZE]]
    left_behind = _highs_it_came_back_from(earlier, recent_prices)
    if len(left_behind) >= 2 and current_price <= max(left_behind):
        return None
    if _back_where_it_was(earlier, recent_prices, current_price):
        return None
    window_start, since = start[0][0], len(start)
    # Everything _recent_move reads: its recent prices and the ones it
    # looks back through.
    reach = RECENT_WINDOW_SIZE + RETURN_LOOKBACK
    for end in range(len(points) - 1, first + short, -1):
        told = _recent_move(points[max(0, end - reach) : end])
        if told is not None:
            baseline_price, window_start, since = told.current_price, told.window_end, told.recent_observation_count
            break
    if current_price - baseline_price < SLOW_MOVE_FACTOR * _threshold(baseline_price):
        return None
    return _Move(baseline_price, current_price, window_start, recent[-1][0], len(recent), since)


def detect_price_creep(db: Session, tenant_id: uuid.UUID) -> list[CreepFinding]:
    """One finding per (tenant, canonical_sku, distributor) whose recent-vs-
    baseline median move clears the larger of the two thresholds above, or
    whose price has risen that far over the past year by smaller steps.
    Read-only — does not write price_alerts (see upsert_creep_alerts for that).

    Per distributor: an increase is one distributor charging more. Pooled,
    a location buying the same cups from Sysco and, lately, from a cheaper
    US Foods had Sysco's 9% rise hidden by US Foods' prices landing in the
    recent window, and whether it alerted depended on how same-day
    deliveries happened to sort. Buying once from a pricier distributor
    would read as an increase the other way round.
    """
    bind_tenant(db, tenant_id)
    observations = db.execute(
        select(
            PriceObservation.canonical_sku_id,
            PriceObservation.distributor_id,
            PriceObservation.observed_on,
            PriceObservation.unit_price_base,
        )
        .where(PriceObservation.tenant_id == tenant_id)
        # id last only so equal dates always sort the same way.
        .order_by(
            PriceObservation.canonical_sku_id,
            PriceObservation.distributor_id,
            PriceObservation.observed_on,
            PriceObservation.id,
        )
    ).all()

    by_series: dict[tuple[uuid.UUID, uuid.UUID], list[tuple[date, Decimal]]] = {}
    for sku_id, distributor_id, observed_on, price in observations:
        by_series.setdefault((sku_id, distributor_id), []).append((observed_on, price))

    findings: list[CreepFinding] = []
    for (sku_id, distributor_id), points in by_series.items():
        move = _recent_move(points) or _slow_move(points)
        if move is None:
            continue
        findings.append(
            CreepFinding(
                canonical_sku_id=sku_id,
                distributor_id=distributor_id,
                baseline_price=move.baseline_price,
                current_price=move.current_price,
                pct_change=((move.current_price - move.baseline_price) / move.baseline_price).quantize(
                    Decimal("0.0001")
                ),
                window_start=move.window_start,
                window_end=move.window_end,
                recent_observation_count=move.recent_observation_count,
                baseline_observation_count=move.baseline_observation_count,
            )
        )
    return findings


def upsert_creep_alerts(db: Session, tenant_id: uuid.UUID) -> list[PriceAlert]:
    """Persists detect_price_creep's findings as open price_alerts rows —
    updates an existing open creep alert for a SKU rather than duplicating it
    on rerun. Feeds Phase 5's Insights page.

    Uses a single atomic INSERT ... ON CONFLICT DO UPDATE (targeting
    price_alerts' partial unique index on (tenant_id, canonical_sku_id,
    distributor_id, alert_type) WHERE status='open' — migrations 0003 and
    0017) rather than a check-then-act SELECT-then-INSERT: two concurrent callers for the same
    tenant (e.g. two review actions landing close together) previously could
    both observe "no open alert yet" and both insert one, producing two open
    creep alerts for the same SKU that only one of them would ever update
    again — a code-review finding on Phase 5.

    Also resolves any open creep alert whose SKU no longer shows creep. This
    function used to only ever add or update, so an alert outlived its cause
    indefinitely: a distributor rolling a price back, a reviewer reopening the
    line whose misread price triggered the alert, or a reseed replacing the
    data underneath it all left the alert open on the Insights page, spectrum
    and all, with no path to clear it. reopen_line_item calls this precisely
    to recompute alerts after deleting a disputed observation, and for the
    alert that observation caused, that recompute did nothing.
    """
    findings = detect_price_creep(db, tenant_id)

    # A person dismissed an alert for this product ("we've dealt with it"):
    # it stays dismissed unless the price climbs clearly past what they saw,
    # rather than reopening on the next delivery at the same price. Keyed by
    # distributor; one dismissed before alerts had a distributor covers the
    # product from any.
    dismissed_at_price: dict[tuple[uuid.UUID, uuid.UUID | None], Decimal] = {}
    for sku_id, distributor_id, price in db.execute(
        select(PriceAlert.canonical_sku_id, PriceAlert.distributor_id, PriceAlert.current_price).where(
            PriceAlert.tenant_id == tenant_id,
            PriceAlert.alert_type == AlertType.creep,
            PriceAlert.status == AlertStatus.dismissed,
        )
    ):
        key = (sku_id, distributor_id)
        dismissed_at_price[key] = max(price, dismissed_at_price.get(key, price))

    def still_news(f: CreepFinding) -> bool:
        seen = [
            dismissed_at_price[key]
            for key in ((f.canonical_sku_id, f.distributor_id), (f.canonical_sku_id, None))
            if key in dismissed_at_price
        ]
        return not seen or f.current_price > max(seen) * (1 + REOPEN_ABOVE_DISMISSED)

    findings = [f for f in findings if still_news(f)]

    # Resolved rather than deleted: the alert was true when raised, and a
    # dismissed-by-the-data history is worth keeping. Only `open` alerts are
    # touched; anything a person already acknowledged or dismissed is theirs.
    # Alerts from before prices were compared per distributor (no
    # distributor) are always resolved here; a still-rising price reopens as
    # a per-distributor alert below, dated as the one it replaces, so the
    # emails don't announce as new an increase people already heard about.
    legacy_opened = dict(
        db.execute(
            select(PriceAlert.canonical_sku_id, PriceAlert.created_at).where(
                PriceAlert.tenant_id == tenant_id,
                PriceAlert.alert_type == AlertType.creep,
                PriceAlert.status == AlertStatus.open,
                PriceAlert.distributor_id.is_(None),
            )
        ).all()
    )
    still_creeping = [(finding.canonical_sku_id, finding.distributor_id) for finding in findings]
    no_longer = [
        PriceAlert.tenant_id == tenant_id,
        PriceAlert.alert_type == AlertType.creep,
        PriceAlert.status == AlertStatus.open,
    ]
    if still_creeping:
        no_longer.append(
            or_(
                PriceAlert.distributor_id.is_(None),
                tuple_(PriceAlert.canonical_sku_id, PriceAlert.distributor_id).not_in(still_creeping),
            )
        )
    db.execute(update(PriceAlert).where(*no_longer).values(status=AlertStatus.resolved))

    if not findings:
        db.commit()
        return []

    values = [
        {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "canonical_sku_id": finding.canonical_sku_id,
            "distributor_id": finding.distributor_id,
            "alert_type": AlertType.creep,
            "baseline_price": finding.baseline_price,
            "current_price": finding.current_price,
            "pct_change": finding.pct_change,
            "window_start": finding.window_start,
            "window_end": finding.window_end,
            "status": AlertStatus.open,
            "created_at": legacy_opened.get(finding.canonical_sku_id, func.now()),
        }
        for finding in findings
    ]

    stmt = pg_insert(PriceAlert).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["tenant_id", "canonical_sku_id", "distributor_id", "alert_type"],
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
