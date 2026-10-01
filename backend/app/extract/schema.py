from typing import Literal

from pydantic import BaseModel

DISTRIBUTOR_VALUES = ("sysco", "us_foods", "gordon", "pfg", "other")
# What was sent. Only invoices and credit memos are read into prices; the
# rest are held (app/workers/tasks.py).
DOCUMENT_TYPES = ("invoice", "credit_memo", "statement", "price_list", "other")
PRICED_DOCUMENT_TYPES = ("invoice", "credit_memo")


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
    # Defaults keep extractions stored before these existed readable. A
    # Literal, so structured output can only answer one of the five: a free
    # string let "receipt" through, which held a real invoice as "not an
    # invoice".
    distributor_name: str | None = None
    document_type: Literal["invoice", "credit_memo", "statement", "price_list", "other"] = "invoice"
    # The restaurant it was delivered or billed to, as printed.
    customer_name: str | None = None
    # Which invoice each page image belongs to (1, 2, 3 in order; 0 for a
    # page that belongs to none), when one file holds several. Only the
    # first is read into the fields here (app/splitting.py).
    page_invoices: list[int] = []
    # One page image shows this invoice and another, separate one (two
    # half-page tickets copied onto one sheet). Only this one is read.
    shares_a_page: bool = False
    invoice_number: str
    invoice_date: str
    delivery_date: str | None = None
    subtotal: str
    tax: str
    total: str
    line_items: list[ExtractedLineItem]
