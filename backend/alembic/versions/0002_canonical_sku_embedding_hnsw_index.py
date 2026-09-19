"""canonical_skus embedding HNSW index

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-19

Adds the HNSW index SPEC.md §4 calls out under "Indexes that matter" for a
database that already ran 0001 before the index was added to the CanonicalSku
model (0001's `Base.metadata.create_all` only picks up schema additions for a
brand-new database, not one that's already at head) — `IF NOT EXISTS` makes
this safe to run whether or not a given database already has it from a reset.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_canonical_skus_embedding_hnsw "
        "ON canonical_skus USING hnsw (description_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_canonical_skus_embedding_hnsw")
