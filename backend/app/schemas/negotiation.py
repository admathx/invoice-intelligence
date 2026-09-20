import uuid
from decimal import Decimal

from pydantic import BaseModel


class NegotiationLineOut(BaseModel):
    canonical_sku_id: uuid.UUID
    canonical_sku_name: str
    current_price: Decimal
    current_price_line_item_id: uuid.UUID
    target_price: Decimal
    peer_distinct_tenant_count: int
    trailing_90d_quantity: Decimal
    quantity_line_item_ids: list[uuid.UUID]
    recoverable_90d: Decimal
    annualized_savings: Decimal


class NegotiationSheetOut(BaseModel):
    lines: list[NegotiationLineOut]
    # Summed server-side in Decimal — SPEC.md §11's money discipline applies
    # just as much to a total shown on the page as to any per-line figure;
    # a client-side sum over stringified Decimals via JS floats risks a
    # rounding drift on the one document a sales rep is meant to scrutinize.
    total_annualized_savings: Decimal
