import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import VolumeTier


class PriceObservation(Base):
    """Denormalized read model for benchmarking. Written after a line item is confirmed."""

    __tablename__ = "price_observations"
    __table_args__ = (
        Index(
            "ix_price_observations_benchmark_cell",
            "canonical_sku_id",
            "metro",
            "volume_tier",
            "observed_on",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    canonical_sku_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_skus.id"), nullable=False
    )
    distributor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("distributors.id"), nullable=False
    )

    observed_on: Mapped[date] = mapped_column(Date, nullable=False)
    unit_price_base: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)

    metro: Mapped[str] = mapped_column(String, nullable=False)
    volume_tier: Mapped[VolumeTier] = mapped_column(Enum(VolumeTier, name="volume_tier"), nullable=False)

    invoice_line_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_line_items.id"), nullable=False, unique=True
    )
