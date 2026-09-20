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
import statistics
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
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
    # Where a caller-supplied price sits in this cell, 0..1 — "you pay more
    # than 78% of comparable businesses." Set only when compute_benchmark(s)
    # was given a subject price. It is a property of the asker's own price
    # rather than a new published quantile, which is why it is safe to show:
    # it reveals nothing about any individual peer that p25/p50/p75 don't.
    subject_percentile: Decimal | None = None


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


def _percentile_rank(sorted_values: list[Decimal], value: Decimal) -> Decimal:
    """The inverse of _percentile: where `value` sits in the distribution, 0..1.

    Ties count as half ("midrank"), so a price identical to every peer's lands
    at 0.50 rather than at 0.00 or 1.00 depending on comparison direction.
    """
    below = sum(1 for v in sorted_values if v < value)
    tied = sum(1 for v in sorted_values if v == value)
    return ((Decimal(below) + Decimal(tied) / 2) / Decimal(len(sorted_values))).quantize(Decimal("0.0001"))


def _account_price(prices: list[Decimal]) -> Decimal:
    """One business's representative price for the window: its median.

    Median rather than mean for the same reason price_creep.py and the
    negotiation sheet's history target use it — one spot buy or promo price
    shouldn't move what this business is recorded as paying.
    """
    return statistics.median(prices).quantize(Decimal("0.0001"))  # median sorts internally


def _cells(
    db: Session,
    canonical_sku_ids: Sequence[uuid.UUID],
    metro: str | None,
    volume_tier: VolumeTier | None,
    window_start: date,
    as_of: date,
    exclude_account_key: uuid.UUID | None,
    subject_prices: Mapping[uuid.UUID, Decimal] | None,
) -> dict[uuid.UUID, BenchmarkResult]:
    """One scope (metro or national) for many SKUs in a single query.

    Batched rather than one query per SKU: the negotiation sheet asks about
    every SKU a tenant buys — 118 of them for a mid-size tenant on the current
    corpus — and a per-SKU call meant up to 236 cross-tenant scans for one page
    load. Suppressed SKUs are simply absent from the returned dict.
    """
    if not canonical_sku_ids:
        return {}

    account_key = _account_key_column()
    conditions = [
        PriceObservation.canonical_sku_id.in_(canonical_sku_ids),
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
        select(PriceObservation.canonical_sku_id, account_key, PriceObservation.unit_price_base)
        .join(Tenant, Tenant.id == PriceObservation.tenant_id)
        .where(*conditions)
    ).all()

    by_sku: dict[uuid.UUID, dict[uuid.UUID, list[Decimal]]] = defaultdict(lambda: defaultdict(list))
    for sku_id, key, price in rows:
        by_sku[sku_id][key].append(price)

    results: dict[uuid.UUID, BenchmarkResult] = {}
    for sku_id, by_account in by_sku.items():
        if len(by_account) < MIN_DISTINCT_ACCOUNTS:
            continue

        # One vote per business, not one per delivery. Percentiles taken over
        # raw observations are weighted by how often each business buys, which
        # stopped matching the count the moment suppression started counting
        # accounts: a five-location group counts once toward the threshold but
        # would contribute five locations' worth of prices to the statistic.
        # A cell of one such group plus five independents paying $10-$14
        # returned a p25 of $20.00 — a "target price" arguing for a rise.
        account_prices = sorted(_account_price(prices) for prices in by_account.values())
        results[sku_id] = BenchmarkResult(
            canonical_sku_id=sku_id,
            p25=_percentile(account_prices, Decimal("0.25")),
            p50=_percentile(account_prices, Decimal("0.50")),
            p75=_percentile(account_prices, Decimal("0.75")),
            distinct_account_count=len(account_prices),
            scope="metro" if metro is not None else "national",
            subject_percentile=(
                _percentile_rank(account_prices, subject_prices[sku_id])
                if subject_prices is not None and sku_id in subject_prices
                else None
            ),
        )
    return results


def compute_benchmarks(
    db: Session,
    canonical_sku_ids: Sequence[uuid.UUID],
    metro: str,
    as_of: date,
    volume_tier: VolumeTier | None = None,
    exclude_account_key: uuid.UUID | None = None,
    subject_prices: Mapping[uuid.UUID, Decimal] | None = None,
) -> dict[uuid.UUID, BenchmarkResult]:
    """Benchmarks for many SKUs at once: two queries total, not two per SKU.

    Each SKU independently suppresses below MIN_DISTINCT_ACCOUNTS and falls
    back metro -> national -> absent. Never falls back to something narrower
    than metro (that would risk identifying a specific competitor, not protect
    against it).

    `exclude_account_key`: pass account_key_for(db, tenant_id) when the caller
    is that tenant asking "how do I compare to peers" (e.g. a negotiation
    sheet) — a "peer" benchmark that includes the asker's own price lets their
    own overpayment partially cancel out of the very comparison meant to
    expose it. Keyed on account, not tenant, for the same reason the count is:
    excluding only the asking location still leaves its sibling locations,
    whose prices are the asker's own company's.
    """
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    cells = _cells(db, canonical_sku_ids, metro, volume_tier, window_start, as_of, exclude_account_key, subject_prices)
    unresolved = [sku_id for sku_id in canonical_sku_ids if sku_id not in cells]
    cells.update(_cells(db, unresolved, None, volume_tier, window_start, as_of, exclude_account_key, subject_prices))
    return cells


def compute_benchmark(
    db: Session,
    canonical_sku_id: uuid.UUID,
    metro: str,
    as_of: date,
    volume_tier: VolumeTier | None = None,
    exclude_account_key: uuid.UUID | None = None,
    subject_price: Decimal | None = None,
) -> BenchmarkResult | None:
    """One SKU, for callers that genuinely have only one (e.g. an insights card
    anchored to its own alert's window). Batching callers use compute_benchmarks.
    """
    return compute_benchmarks(
        db,
        [canonical_sku_id],
        metro,
        as_of,
        volume_tier,
        exclude_account_key,
        subject_prices={canonical_sku_id: subject_price} if subject_price is not None else None,
    ).get(canonical_sku_id)
