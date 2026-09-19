import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base
from app.models.enums import BaseUom

EMBEDDING_DIM = 384


class CanonicalSku(Base):
    __tablename__ = "canonical_skus"
    __table_args__ = (
        # SPEC.md §4: "Indexes that matter: ... an HNSW index on
        # canonical_skus.description_embedding" — Phase 3's embedding matcher
        # queries this via cosine distance.
        Index(
            "ix_canonical_skus_embedding_hnsw",
            "description_embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"description_embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Unique: there's no separate slug column, so `name` is the stable identifier
    # app/normalize/catalog.py and the synthetic corpus's ground truth both rely
    # on as a cross-reference key — that reliance needs a DB-level guarantee, not
    # just seed_canonical_skus()'s application-level dedup.
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String, nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String, nullable=True)

    base_uom: Mapped[BaseUom] = mapped_column(Enum(BaseUom, name="base_uom"), nullable=False)
    gtin: Mapped[str | None] = mapped_column(String, nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)

    description_embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
