import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    raw_description: str
    raw_sku: str | None
    raw_pack_size: str | None
    # The pack is one a person entered for this item, not from the page.
    pack_size_remembered: bool = False
    # What the invoice printed, when an entered or remembered pack replaced it.
    printed_pack_size: str | None = None
    quantity: Decimal
    unit_price: Decimal
    extended_price: Decimal
    uom: str
    canonical_sku_id: uuid.UUID | None
    # Which product it was matched to, so a person can see a wrong match.
    canonical_sku_name: str | None = None
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
    # Held for a person: likely a copy of another invoice, or not an invoice
    # at all (app/workers/tasks.py).
    duplicate_of_id: uuid.UUID | None = None
    document_type: str | None = None
    printed_distributor: str | None = None
    # Made out to what looks like another restaurant (app/billed_to.py):
    # also held.
    printed_customer: str | None = None
    billed_elsewhere: bool = False
    # The invoice whose file this one came out of (app/splitting.py).
    split_from_id: uuid.UUID | None = None


class InvoiceCheckOut(BaseModel):
    """Whether the invoice's numbers can be trusted, and if not, why.

    `passes` is what the confirm endpoint requires; `reasons` is what the
    review screen shows a person so they know what to look for on the page.
    """

    passes: bool
    reasons: list[str]
    failed_line_numbers: list[int]


class InvoiceDetailOut(InvoiceOut):
    distributor_name: str | None = None
    # The invoice this looks like a copy of, as the screen names it.
    duplicate_of_label: str | None = None
    # That invoice is the one this was split out of: these are likely more
    # of its pages, not a copy.
    duplicate_is_same_file: bool = False
    # Said on the screen when the file held more than one invoice.
    split_note: str | None = None
    line_items: list[LineItemOut] = []
    page_image_urls: list[str] = []
    check: InvoiceCheckOut


# Same shape as the Numeric(12, 4) columns these land in, so an out-of-range
# value is a 422 at the edge rather than a database error mid-commit.
Money = Annotated[Decimal, Field(max_digits=12, decimal_places=4)]


class LineItemEdit(BaseModel):
    id: uuid.UUID
    quantity: Money | None = None
    unit_price: Money | None = None
    extended_price: Money | None = None
    # What identifies the item, correctable because OCR misreads these too.
    # The arithmetic check never looks at them, so a misread pack size ('4/3 LB'
    # for '4/5 LB') passed review and confirmed a per-pound price 67% too high.
    # Empty string clears item code / pack size; description and unit can't be
    # blank.
    raw_description: str | None = Field(default=None, min_length=1, max_length=500)
    raw_sku: str | None = Field(default=None, max_length=100)
    raw_pack_size: str | None = Field(default=None, max_length=100)
    uom: str | None = Field(default=None, min_length=1, max_length=20)


class LineItemCreate(BaseModel):
    """A line typed in by hand from the invoice image, when extraction missed
    it or found nothing at all. Every printed number is required: the invoice
    is checked with the same arithmetic as extracted lines, so nothing here is
    derived (no extended = qty x price) that the check would then trivially
    agree with."""

    raw_description: str = Field(min_length=1, max_length=500)
    raw_sku: str | None = Field(default=None, max_length=100)
    raw_pack_size: str | None = Field(default=None, max_length=100)
    uom: str = Field(min_length=1, max_length=20)
    quantity: Money
    unit_price: Money
    extended_price: Money


class LineItemSuggestion(BaseModel):
    """A line this tenant has bought before, offered while entering one by hand.

    Carries the last price only as a hint for the person typing (see
    InvoiceReview.tsx): prefilling it would let an unchanged field record
    "no increase" and erase the very creep the product exists to catch.
    """

    raw_description: str
    raw_sku: str | None
    raw_pack_size: str | None
    uom: str
    canonical_sku_name: str | None
    last_unit_price: Decimal
    last_seen: date


class InvoiceEdit(BaseModel):
    """Corrections a person makes while reading the invoice page. Only fields
    actually sent are applied; there is no way to blank a value from here."""

    distributor_id: uuid.UUID | None = None
    invoice_date: date | None = None
    subtotal: Money | None = None
    tax: Money | None = None
    total: Money | None = None
    line_items: list[LineItemEdit] = []


class InvoiceUploadResponse(BaseModel):
    id: uuid.UUID
    status: InvoiceStatus
