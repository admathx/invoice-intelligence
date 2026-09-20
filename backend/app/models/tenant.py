import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base
from app.models.enums import VolumeTier


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # The business this location belongs to, when it belongs to a multi-unit
    # one. NULL means "this tenant is its own business" — the common case, and
    # what every row created before accounts existed means. Benchmarking counts
    # distinct accounts rather than distinct tenants so a group can't satisfy
    # the suppression threshold with its own locations; see app/models/account.py.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True, index=True
    )
    # The address a restaurant forwards its invoices to (SPEC.md §10 Phase 6's
    # "per-tenant address routing"). Nullable because a tenant can exist before
    # anyone hands them an address; unique because it IS the routing key —
    # two tenants sharing one would make inbound mail ambiguous.
    inbox_address: Mapped[str | None] = mapped_column(String, nullable=True, unique=True, index=True)
    metro: Mapped[str] = mapped_column(String, nullable=False)
    volume_tier: Mapped[VolumeTier] = mapped_column(Enum(VolumeTier, name="volume_tier"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


def account_key_column(entity=Tenant):
    """A tenant's business identity: its account when it has one, else itself.

    COALESCE rather than a NULL-safe join to a backfilled account row, so that
    single-location tenants (the overwhelming majority, and every row that
    predates accounts) need no Account row at all and count exactly as they
    always did.

    Lives on the model rather than in either caller because two unrelated
    layers need the same notion of "independent business": benchmarking, for
    how many businesses stand behind a cell, and alias promotion, for how many
    businesses independently confirmed a match. `entity` takes an alias when
    the query joins tenants under another name.
    """
    return func.coalesce(entity.account_id, entity.id)
