import uuid
from decimal import Decimal

from sqlalchemy import Enum, ForeignKey, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TenantScoped
from app.models.enums import BaseUom, ReviewStatus


class InvoiceLineItem(Base, TenantScoped):
    __tablename__ = "invoice_line_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # tenant_id comes from the TenantScoped mixin.
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id"), nullable=False, index=True
    )

    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Verbatim from the invoice. Never mutated after extraction — corrections write
    # to the normalized columns below, not here.
    raw_description: Mapped[str] = mapped_column(String, nullable=False)
    raw_sku: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_pack_size: Mapped[str | None] = mapped_column(String, nullable=True)

    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    extended_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    uom: Mapped[str] = mapped_column(String, nullable=False)

    # Normalized
    canonical_sku_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_skus.id"), nullable=True, index=True
    )
    normalized_qty_base: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    normalized_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    base_uom: Mapped[BaseUom | None] = mapped_column(Enum(BaseUom, name="base_uom"), nullable=True)

    # QA
    extraction_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    review_status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus, name="review_status"), nullable=False, default=ReviewStatus.pending
    )
