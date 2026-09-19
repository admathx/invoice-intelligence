import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ReviewQueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_date: date | None
    distributor_name: str | None
    raw_description: str
    raw_sku: str | None
    raw_pack_size: str | None
    quantity: Decimal
    unit_price: Decimal
    uom: str
    canonical_sku_id: uuid.UUID | None
    canonical_sku_name: str | None
    match_confidence: Decimal | None


class CorrectRequest(BaseModel):
    canonical_sku_id: uuid.UUID


class ReviewActionResponse(BaseModel):
    id: uuid.UUID
    review_status: str
    canonical_sku_id: uuid.UUID | None
    normalized_unit_price: Decimal | None
    wrote_alias: bool
    wrote_price_observation: bool
