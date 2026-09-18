import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base
from app.models.enums import InvoiceSource, InvoiceStatus


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
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
    original_file_uri: Mapped[str] = mapped_column(String, nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, name="invoice_status"), nullable=False, default=InvoiceStatus.received
    )

    extraction_model: Mapped[str | None] = mapped_column(String, nullable=True)
    extraction_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    extracted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
