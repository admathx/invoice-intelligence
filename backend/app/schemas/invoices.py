import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    raw_description: str
    raw_sku: str | None
    raw_pack_size: str | None
    quantity: Decimal
    unit_price: Decimal
    extended_price: Decimal
    uom: str
    canonical_sku_id: uuid.UUID | None
    normalized_qty_base: Decimal | None
    normalized_unit_price: Decimal | None
    base_uom: BaseUom | None
    extraction_confidence: Decimal | None
    match_confidence: Decimal | None
    review_status: ReviewStatus


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    distributor_id: uuid.UUID | None
    invoice_number: str | None
    invoice_date: date | None
    delivery_date: date | None
    subtotal: Decimal | None
    tax: Decimal | None
    total: Decimal | None
    source: InvoiceSource
    status: InvoiceStatus
    extraction_model: str | None
    extraction_cost_usd: Decimal | None
    extracted_at: datetime | None
    created_at: datetime


class InvoiceDetailOut(InvoiceOut):
    line_items: list[LineItemOut] = []


class InvoiceUploadResponse(BaseModel):
    id: uuid.UUID
    status: InvoiceStatus
