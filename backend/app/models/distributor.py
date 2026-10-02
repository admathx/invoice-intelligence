import uuid

from sqlalchemy import Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _name_key(name: str) -> str:
    from app.business_distributors import name_key  # the one definition

    return name_key(name)


class Distributor(Base):
    __tablename__ = "distributors"
    __table_args__ = (
        Index("ix_distributors_name_key", "name_key"),
        # Once per business: two people adding the same vendor at once can't
        # create it twice (migration 0021).
        Index(
            "ix_distributors_business_name_unique",
            "account_key",
            "name_key",
            unique=True,
            postgresql_where=text("account_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # The business that added it (account_key_column: its account, or the
    # location itself), for a local vendor only that business buys from. Null
    # for the shared distributors everyone sees.
    account_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    # Where to find them, linked from a price alert that says a product costs
    # less there (app/analytics/alternatives.py). Null for most vendors a
    # business added itself.
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    # The name reduced to what identifies it (app/business_distributors.py
    # name_key), set from name when the row is added (names aren't edited).
    name_key: Mapped[str | None] = mapped_column(
        String, nullable=True, default=lambda ctx: _name_key(ctx.get_current_parameters()["name"])
    )


# Seeded slugs (see alembic seed data / synthetic generator):
SEED_SLUGS = ["sysco", "us_foods", "gordon", "pfg", "other"]

# Extraction's "I couldn't tell whose invoice this is" answer. It is a real row
# (the worker stores it rather than NULL), but it is not a distributor: item
# codes only mean something within one real distributor's catalog, so an
# invoice attributed here can't be matched or confirmed.
UNRECOGNIZED_SLUG = "other"
