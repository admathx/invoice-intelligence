from pydantic import BaseModel

DISTRIBUTOR_VALUES = ("sysco", "us_foods", "gordon", "pfg", "other")


class ExtractedLineItem(BaseModel):
    line_number: int
    raw_sku: str | None = None
    raw_description: str
    raw_pack_size: str | None = None
    quantity: str
    uom: str
    unit_price: str
    extended_price: str
    confidence: float


class ExtractedInvoice(BaseModel):
    distributor: str
    invoice_number: str
    invoice_date: str
    delivery_date: str | None = None
    subtotal: str
    tax: str
    total: str
    line_items: list[ExtractedLineItem]
