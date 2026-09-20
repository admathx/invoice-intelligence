import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base, TenantScoped
from app.models.enums import InvoiceSource, InvoiceStatus


class Invoice(Base, TenantScoped):
    __tablename__ = "invoices"

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
    original_file_uri: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.received
    )

    extraction_model: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    extracted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
