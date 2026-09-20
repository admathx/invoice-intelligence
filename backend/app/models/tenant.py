import uuid
from datetime import datetime

from sqlalchemy import Enum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base
from app.models.enums import VolumeTier


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The address a restaurant forwards its invoices to (SPEC.md §10 Phase 6's
    # "per-tenant address routing"). Nullable because a tenant can exist before
    # anyone hands them an address; unique because it IS the routing key —
    # two tenants sharing one would make inbound mail ambiguous.
    inbox_address: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    metro: Mapped[str] = mapped_column(String, nullable=False)
    volume_tier: Mapped[VolumeTier] = mapped_column(Enum(VolumeTier, name="volume_tier"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
