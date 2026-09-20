import uuid
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class Account(Base):
    """The business that owns one or more tenants (locations).

    Exists for exactly one reason: SPEC.md §7's suppression rule counts
    *distinct tenants*, and a tenant is a location. A five-location group
    onboarded as five tenants therefore clears the five-tenant privacy
    threshold using nothing but its own locations — the "peer benchmark" it
    gets back is the group compared against itself, which is both a wrong
    number and a silent violation of what the rule is for. Benchmark cells
    count distinct *accounts* instead (see app/analytics/benchmark.py), so a
    group counts once no matter how many locations it has.

    Deliberately thin. It is a grouping key, not a customer record: billing,
    contacts and plan live wherever they end up living, and nothing in the
    analytics path should need more than "which tenants are the same
    business." Tenants with no account_id are their own account (see
    account_key_for), so single-location customers need no Account row and
    every existing row keeps behaving exactly as before.
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
