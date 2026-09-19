import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class CanonicalSkuSearchResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str
    subcategory: str | None
    base_uom: str


class SkuMatchedLine(BaseModel):
    invoice_id: uuid.UUID
    invoice_date: date | None
    distributor_name: str
    raw_description: str
    normalized_unit_price: Decimal | None
    match_confidence: Decimal | None
    review_status: str


class CanonicalSkuDetail(CanonicalSkuSearchResult):
    matched_lines: list[SkuMatchedLine]
