import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class SkuAlias(Base):
    """Every human correction writes a row here. This table IS the moat.

    Deliberately NOT TenantScoped (app.db.TenantScoped), despite having a
    tenant_id: the whole point of the promotion rule below is to read across
    tenants and count how many independent businesses confirmed the same
    mapping. A tenant-scope filter would hide every other tenant's rows and
    silently reduce promotion to "my own corrections only" — the guard would
    quietly disable the feature rather than protect anything, since nothing
    here is a price.
    """

    __tablename__ = "sku_aliases"
    __table_args__ = (
        # Alias lookup is an exact match on (distributor_id, raw_sku) — the fast path
        # that must stay O(1) as this table grows with every human correction.
        Index("ix_sku_aliases_distributor_raw_sku", "distributor_id", "raw_sku"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Who made this correction. NULL means a system-curated alias (a catalog
    # import, not one user's judgement call), which is trusted everywhere
    # without needing a second confirmation. A correction from a real review
    # queue always carries the tenant that made it — see
    # app/normalize/matcher.py's match_by_alias for what that gates.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=True, index=True
    )
    canonical_sku_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_skus.id"), nullable=False, index=True
    )
    distributor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("distributors.id"), nullable=False, index=True
    )

    raw_description: Mapped[str] = mapped_column(String, nullable=False)
    raw_sku: Mapped[str | None] = mapped_column(String, nullable=True)
    pack_size: Mapped[str | None] = mapped_column(String, nullable=True)

    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
