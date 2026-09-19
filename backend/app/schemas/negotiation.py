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
