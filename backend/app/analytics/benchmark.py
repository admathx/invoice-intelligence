"""SPEC.md §7: peer benchmarking, with suppression as a hard privacy rule, not
a tunable — "never show an identifiable competitor's price" (SPEC.md §1).

`volume_tier` is a query parameter the caller opts into (default None = not
fragmented), per SPEC.md §12's open question: "does volume tier belong in
benchmark cells, or does it fragment the data too much early on? Leaning:
make it a query parameter, default off until tenant density supports it."
"""
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.enums import VolumeTier
from app.models.price_observation import PriceObservation

LOOKBACK_DAYS = 90
MIN_DISTINCT_TENANTS = 5  # SPEC.md §7: "a hard rule, not a tunable"


@dataclass
class BenchmarkResult:
    canonical_sku_id: uuid.UUID
    p25: Decimal
    p50: Decimal
    p75: Decimal
    distinct_tenant_count: int
    scope: str  # "metro" | "national"


def _percentile(sorted_values: list[Decimal], pct: Decimal) -> Decimal:
    """Linear-interpolation percentile (numpy's default 'linear' method), done
    entirely in Decimal — SPEC.md §11: money is Decimal, never floats, and a
    benchmark price is exactly the kind of number that ends up on a
    negotiation sheet a rep will scrutinize.
    """
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    rank = pct * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _cell(
    db: Session, canonical_sku_id: uuid.UUID, metro: str | None, volume_tier: VolumeTier | None, window_start: date, as_of: date
) -> BenchmarkResult | None:
    conditions = [
        PriceObservation.canonical_sku_id == canonical_sku_id,
        PriceObservation.observed_on >= window_start,
        PriceObservation.observed_on <= as_of,
    ]
    if metro is not None:
        conditions.append(PriceObservation.metro == metro)
    if volume_tier is not None:
        conditions.append(PriceObservation.volume_tier == volume_tier)

    rows = db.execute(select(PriceObservation.tenant_id, PriceObservation.unit_price_base).where(*conditions)).all()
    distinct_tenants = {tenant_id for tenant_id, _ in rows}
    if len(distinct_tenants) < MIN_DISTINCT_TENANTS:
        return None

    prices = sorted(price for _, price in rows)
    return BenchmarkResult(
        canonical_sku_id=canonical_sku_id,
        p25=_percentile(prices, Decimal("0.25")),
        p50=_percentile(prices, Decimal("0.50")),
        p75=_percentile(prices, Decimal("0.75")),
        distinct_tenant_count=len(distinct_tenants),
        scope="metro" if metro is not None else "national",
    )


def compute_benchmark(
    db: Session,
    canonical_sku_id: uuid.UUID,
    metro: str,
    as_of: date,
    volume_tier: VolumeTier | None = None,
) -> BenchmarkResult | None:
    """Suppresses the cell below MIN_DISTINCT_TENANTS, falls back metro ->
    national -> None. Never falls back to something narrower than metro (that
    would risk identifying a specific competitor, not protect against it).
    """
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    return _cell(db, canonical_sku_id, metro, volume_tier, window_start, as_of) or _cell(
        db, canonical_sku_id, None, volume_tier, window_start, as_of
    )
