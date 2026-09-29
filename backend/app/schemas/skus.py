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
    # The item itself, so a wrong match can be sent back to be matched again.
    line_id: uuid.UUID
    invoice_id: uuid.UUID
    invoice_date: date | None
    distributor_name: str
    raw_description: str
    normalized_unit_price: Decimal | None
    match_confidence: Decimal | None
    review_status: str


class CanonicalSkuDetail(CanonicalSkuSearchResult):
    matched_lines: list[SkuMatchedLine]
