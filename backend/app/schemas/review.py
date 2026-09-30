import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
    # Whether a price per pound, gallon or item could be worked out from the
    # pack size and unit. When not, matching still records what the item is,
    # but its price isn't tracked until a pack size is entered for it.
    price_known: bool
    # The same item waiting on this many invoices' lines, this one included.
    # One decision settles them all (app/api/review.py _settle_repeats).
    count: int = 1


class CorrectRequest(BaseModel):
    canonical_sku_id: uuid.UUID


class ReviewActionResponse(BaseModel):
    id: uuid.UUID
    review_status: str
    canonical_sku_id: uuid.UUID | None
    normalized_unit_price: Decimal | None
    wrote_alias: bool
    wrote_price_observation: bool
    # Other waiting lines of the same item settled by the same decision.
    also_settled: int = 0


class AcceptSuggestions(BaseModel):
    # Never below the floor at which a suggestion is shown at all.
    min_confidence: Decimal = Field(default=Decimal("0.85"), ge=Decimal("0.60"), le=Decimal("1"))


class AcceptSuggestionsResponse(BaseModel):
    items: int
    lines: int


class PackSizeRequest(BaseModel):
    pack_size: str = Field(min_length=1, max_length=40)
    # The billing unit, when that's what was wrong ("EA" that is really the
    # case). Left as it is when omitted.
    uom: str | None = Field(default=None, min_length=1, max_length=12)


class PackSizeResponse(BaseModel):
    id: uuid.UUID
    raw_pack_size: str
    uom: str
    review_status: str
    normalized_unit_price: Decimal | None
    price_known: bool
    # Remembered for the item, and filled into this many of its other lines.
    remembered: bool
    applied_to: int
