"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-18

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db import Base
from app.models import *  # noqa: F401,F403  (registers all tables on Base.metadata)
from app.models.distributor import SEED_SLUGS

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    distributors = sa.table(
        "distributors",
        sa.column("id", sa.dialects.postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
    )
    op.bulk_insert(
        distributors,
        [
            {"id": uuid.uuid4(), "name": slug.replace("_", " ").title(), "slug": slug}
            for slug in SEED_SLUGS
        ],
    )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
    op.execute("DROP EXTENSION IF EXISTS vector")
