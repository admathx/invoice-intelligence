import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class PriceHistoryPoint(BaseModel):
    observed_on: date
    unit_price_base: Decimal


class BenchmarkPosition(BaseModel):
    p25: Decimal
    p50: Decimal
    p75: Decimal
    tenant_price: Decimal
    # Independent businesses in the cell, not locations — a multi-unit group
    # counts once (app/analytics/benchmark.py).
    distinct_account_count: int
    scope: str  # "metro" | "national"


class InsightCard(BaseModel):
    alert_id: uuid.UUID
    canonical_sku_id: uuid.UUID
    canonical_sku_name: str
    alert_type: str
    baseline_price: Decimal
    current_price: Decimal
    pct_change: Decimal
    window_start: date
    window_end: date
    status: str
    price_history: list[PriceHistoryPoint]
    benchmark: BenchmarkPosition | None
