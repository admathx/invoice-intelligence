import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class RememberedPack(Base):
    """A pack size a person entered for an item whose invoices print none (or
    one that can't be read), used for that item from then on (app/packs.py).

    Per business (account_key_column: its account, or a location with none)
    rather than per location: the same distributor sells the same item code
    in the same pack to every location of a business. Not TenantScoped for
    the same reason."""

    __tablename__ = "remembered_packs"
    __table_args__ = (UniqueConstraint("account_key", "distributor_id", "item_key", name="uq_remembered_packs_item"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    distributor_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("distributors.id"), nullable=False)
    # The item: "sku:<item code>", or "desc:<description>" when there's no code.
    item_key: Mapped[str] = mapped_column(String, nullable=False)
    pack_size: Mapped[str] = mapped_column(String, nullable=False)
    set_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
