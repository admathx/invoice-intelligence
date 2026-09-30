import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Distributor(Base):
    __tablename__ = "distributors"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # The business that added it (account_key_column: its account, or the
    # location itself), for a local vendor only that business buys from. Null
    # for the shared distributors everyone sees.
    account_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)


# Seeded slugs (see alembic seed data / synthetic generator):
SEED_SLUGS = ["sysco", "us_foods", "gordon", "pfg", "other"]

# Extraction's "I couldn't tell whose invoice this is" answer. It is a real row
# (the worker stores it rather than NULL), but it is not a distributor: item
# codes only mean something within one real distributor's catalog, so an
# invoice attributed here can't be matched or confirmed.
UNRECOGNIZED_SLUG = "other"
