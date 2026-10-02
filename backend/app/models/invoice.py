import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Index, Numeric, String, false
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base, TenantScoped
from app.models.enums import InvoiceSource, InvoiceStatus


class Invoice(Base, TenantScoped):
    __tablename__ = "invoices"
    # An identical file is looked up by hash at upload (migration 0020).
    __table_args__ = (Index("ix_invoices_tenant_file_sha256", "tenant_id", "file_sha256"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # tenant_id comes from the TenantScoped mixin.
    distributor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("distributors.id"), nullable=True
    )

    invoice_number: Mapped[str | None] = mapped_column(String, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delivery_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    tax: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)

    source: Mapped[InvoiceSource] = mapped_column(Enum(InvoiceSource, name="invoice_source"), nullable=False)
    # RFC 5322 Message-ID of the email this invoice arrived in, for email
    # sources. The idempotency key for intake: mail delivery retries, and an
    # operator re-dropping a quarantined email after fixing a tenant address
    # is the documented recovery, so without it the same PDF becomes a second
    # invoice and double-counts into every benchmark cell and creep window it
    # feeds. NULL for uploads, which are a deliberate human action each time.
    source_message_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    # Empty for an invoice a person typed in: there is no file.
    original_file_uri: Mapped[str] = mapped_column(String, nullable=False)
    # SHA-256 of the stored file: an identical file is refused at upload.
    file_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    # Another invoice from the same distributor with the same number: this is
    # likely a copy (a rescan, or forwarded twice). Held until a person
    # deletes it or says it's a different invoice.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True
    )
    # Set when what was sent isn't an invoice ("statement", "price_list",
    # "other"): held, and nothing on it is used. Null for invoices and
    # credit memos.
    document_type: Mapped[str | None] = mapped_column(String, nullable=True)
    # The distributor's name as printed, whatever it matched to.
    printed_distributor: Mapped[str | None] = mapped_column(String, nullable=True)
    # The restaurant it was delivered or billed to, as printed.
    printed_customer: Mapped[str | None] = mapped_column(String, nullable=True)
    # That name is another restaurant's (app/billed_to.py): held until a
    # person deletes it or says it's theirs.
    billed_elsewhere: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # Its page also shows another invoice, which wasn't read (two tickets
    # copied onto one sheet): kept for a person, who adds the other one.
    shares_page: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # The invoice whose file this one was taken out of, when one file held
    # several invoices (app/splitting.py).
    split_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.received
    )

    extraction_model: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    extracted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)

    @property
    def is_held(self) -> bool:
        """Held whatever its numbers say, with nothing on it used or matched
        until a person settles it: a likely copy, not an invoice, or another
        restaurant's."""
        return self.duplicate_of_id is not None or self.document_type is not None or self.billed_elsewhere


def not_held():
    """Invoice.is_held, negated, as SQL conditions."""
    return (Invoice.duplicate_of_id.is_(None), Invoice.document_type.is_(None), Invoice.billed_elsewhere.is_(False))
