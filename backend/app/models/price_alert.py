import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base, TenantScoped
from app.models.enums import AlertStatus, AlertType


class PriceAlert(Base, TenantScoped):
    __tablename__ = "price_alerts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # tenant_id comes from the TenantScoped mixin.
    canonical_sku_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_skus.id"), nullable=False
    )

    alert_type: Mapped[AlertType] = mapped_column(Enum(AlertType, name="alert_type"), nullable=False)

    baseline_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    current_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    pct_change: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)

    peer_median: Mapped[Decimal | None] = mapped_column(Numeric(12, 4), nullable=True)
    peer_percentile: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)

    status: Mapped[AlertStatus] = mapped_column(
        Enum(AlertStatus, name="alert_status"), nullable=False, default=AlertStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
