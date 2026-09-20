import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, Enum, ForeignKey, Index, Numeric, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base, TenantScoped
from app.models.enums import AlertStatus, AlertType


class PriceAlert(Base, TenantScoped):
    __tablename__ = "price_alerts"
    __table_args__ = (
        # At most one OPEN alert per (tenant, sku, type) — backs
        # upsert_creep_alerts' check-then-act against concurrent callers.
        # A closed/acknowledged/resolved alert for the same SKU is
        # legitimate history, not a duplicate, hence the partial WHERE.
        # Alembic migration 0003 applies this to databases that already
        # ran 0001/0002 before this constraint existed.
        Index(
            "ix_price_alerts_open_unique",
            "tenant_id",
            "canonical_sku_id",
            "alert_type",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

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
