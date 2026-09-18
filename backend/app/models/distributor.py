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


# Seeded slugs (see alembic seed data / synthetic generator):
SEED_SLUGS = ["sysco", "us_foods", "gordon", "pfg", "other"]
