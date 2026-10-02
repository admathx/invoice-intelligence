import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class PriceHistoryPoint(BaseModel):
    observed_on: date
    unit_price_base: Decimal


class BenchmarkPosition(BaseModel):
    p25: Decimal
    p50: Decimal
    p75: Decimal
    tenant_price: Decimal
    # Where tenant_price sits among peers, 0..1. The one number that answers
    # "is this bad?" without the reader having to compare four dollar figures
    # in their head.
    percentile: Decimal
    # Independent businesses in the cell, not locations — a multi-unit group
    # counts once (app/analytics/benchmark.py).
    distinct_account_count: int
    scope: str  # "metro" | "national"


class AlternativeOut(BaseModel):
    """Another distributor the alert's product costs less at
    (app/analytics/alternatives.py)."""

    model_config = ConfigDict(from_attributes=True)

    distributor_name: str
    price: Decimal
    # How much less than the alert's current price, 0..1.
    saving_pct: Decimal
    # This location's own price there; otherwise what others typically pay.
    yours: bool
    last_bought: date | None = None
    distinct_account_count: int | None = None
    scope: str | None = None  # "metro" | "national"


class InsightCard(BaseModel):
    alert_id: uuid.UUID
    canonical_sku_id: uuid.UUID
    canonical_sku_name: str
    # Whose price went up; None on alerts from before that was recorded.
    distributor_name: str | None = None
    alert_type: str
    baseline_price: Decimal
    current_price: Decimal
    pct_change: Decimal
    window_start: date
    window_end: date
    status: str
    price_history: list[PriceHistoryPoint]
    benchmark: BenchmarkPosition | None
    # Cheapest first; empty when nowhere else is known to cost less.
    alternatives: list[AlternativeOut] = []
