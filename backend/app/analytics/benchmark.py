"""SPEC.md §7: peer benchmarking, with suppression as a hard privacy rule, not
a tunable — "never show an identifiable competitor's price" (SPEC.md §1).

Suppression counts distinct *accounts*, not distinct tenants. SPEC.md words
the rule as "5 distinct tenants" because it assumes one restaurant per
customer, but a tenant is a location: a five-location group onboarded as five
tenants would clear the threshold using nothing but its own locations, and the
"peer benchmark" it got back would be the group compared against itself — a
wrong number and a silent defeat of the rule at the same time. Tenants with no
account_id are their own account (see account_key_for), so for every
single-location customer this is identical to counting tenants.

`volume_tier` is a query parameter the caller opts into (default None = not
fragmented), per SPEC.md §12's open question: "does volume tier belong in
benchmark cells, or does it fragment the data too much early on? Leaning:
make it a query parameter, default off until tenant density supports it."
"""
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.enums import VolumeTier
from app.models.price_observation import PriceObservation
from app.models.tenant import Tenant

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_benchmark_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase4_analytics"]["benchmark"]

# Read from validation/thresholds.yaml rather than hardcoded here and mirrored
# by comment — the Phase 3 review found that pattern lets the live code drift
# from what thresholds.yaml documents when only one side is edited. Loading it
# does not make it tunable: it is the same hard rule either way, and
# thresholds.yaml labels it as one.
LOOKBACK_DAYS = _benchmark_thresholds["lookback_days"]
MIN_DISTINCT_ACCOUNTS = _benchmark_thresholds["min_distinct_tenants"]  # SPEC.md §7: "a hard rule, not a tunable"


@dataclass
class BenchmarkResult:
    canonical_sku_id: uuid.UUID
    p25: Decimal
    p50: Decimal
    p75: Decimal
    distinct_account_count: int
    scope: str  # "metro" | "national"


def _account_key_column():
    """A tenant's business identity: its account when it has one, else itself.

    COALESCE rather than a NULL-safe join to a backfilled account row, so that
    single-location tenants (the overwhelming majority, and every row that
    predates accounts) need no Account row at all and count exactly as they
    always did.
    """
    return func.coalesce(Tenant.account_id, Tenant.id)


def account_key_for(db: Session, tenant_id: uuid.UUID) -> uuid.UUID:
    """The account key to exclude when `tenant_id` is the one asking.

    Resolved by the caller rather than inside compute_benchmark because both
    callers ask for many SKUs in a loop (a negotiation sheet, an insights
    page), and the asker's account doesn't change between SKUs.
    """
    key = db.scalar(select(_account_key_column()).where(Tenant.id == tenant_id))
    # A tenant_id with no tenants row can't have siblings, so excluding the id
    # itself is exactly right (and keeps this total rather than raising on a
    # caller that already 404s on the same condition).
    return key if key is not None else tenant_id


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
    return (sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac).quantize(Decimal("0.0001"))


def _cell(
    db: Session,
    canonical_sku_id: uuid.UUID,
    metro: str | None,
    volume_tier: VolumeTier | None,
    window_start: date,
    as_of: date,
    exclude_account_key: uuid.UUID | None,
) -> BenchmarkResult | None:
    account_key = _account_key_column()
    conditions = [
        PriceObservation.canonical_sku_id == canonical_sku_id,
        PriceObservation.observed_on >= window_start,
        PriceObservation.observed_on <= as_of,
    ]
    if metro is not None:
        conditions.append(PriceObservation.metro == metro)
    if volume_tier is not None:
        conditions.append(PriceObservation.volume_tier == volume_tier)
    if exclude_account_key is not None:
        conditions.append(account_key != exclude_account_key)

    # Joined to tenants rather than denormalizing account_id onto
    # price_observations alongside metro/volume_tier: those are snapshots of
    # what was true when the line was billed, which is what a benchmark cell
    # wants, but account membership is a privacy fact that must be current.
    # A group acquiring a restaurant today has to stop being that restaurant's
    # "peer" for prices observed last month too, and a denormalized column
    # would keep quietly counting them separately. tenants is small and this
    # is a hash join on an indexed FK.
    rows = db.execute(
        select(account_key, PriceObservation.unit_price_base)
        .join(Tenant, Tenant.id == PriceObservation.tenant_id)
        .where(*conditions)
    ).all()

    distinct_accounts = {key for key, _ in rows}
    if len(distinct_accounts) < MIN_DISTINCT_ACCOUNTS:
        return None

    prices = sorted(price for _, price in rows)
    return BenchmarkResult(
        canonical_sku_id=canonical_sku_id,
        p25=_percentile(prices, Decimal("0.25")),
        p50=_percentile(prices, Decimal("0.50")),
        p75=_percentile(prices, Decimal("0.75")),
        distinct_account_count=len(distinct_accounts),
        scope="metro" if metro is not None else "national",
    )


def compute_benchmark(
    db: Session,
    canonical_sku_id: uuid.UUID,
    metro: str,
    as_of: date,
    volume_tier: VolumeTier | None = None,
    exclude_account_key: uuid.UUID | None = None,
) -> BenchmarkResult | None:
    """Suppresses the cell below MIN_DISTINCT_ACCOUNTS, falls back metro ->
    national -> None. Never falls back to something narrower than metro (that
    would risk identifying a specific competitor, not protect against it).

    `exclude_account_key`: pass account_key_for(db, tenant_id) when the caller
    is that tenant asking "how do I compare to peers" (e.g. a negotiation
    sheet) — a "peer" benchmark that includes the asker's own price lets their
    own overpayment partially cancel out of the very comparison meant to
    expose it. Keyed on account, not tenant, for the same reason the count is:
    excluding only the asking location still leaves its sibling locations,
    whose prices are the asker's own company's.
    """
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    return _cell(db, canonical_sku_id, metro, volume_tier, window_start, as_of, exclude_account_key) or _cell(
        db, canonical_sku_id, None, volume_tier, window_start, as_of, exclude_account_key
    )
