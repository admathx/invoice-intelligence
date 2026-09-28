"""weekly digest: per-user opt-out, and a record of what was sent

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-28

digest_sends makes sending idempotent: one row per person per week, unique,
so a scheduler restart or a second run can never send the same week twice.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("digest_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "digest_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("week_of", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("location_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("user_id", "week_of", name="uq_digest_sends_user_week"),
    )


def downgrade() -> None:
    op.drop_table("digest_sends")
    op.drop_column("users", "digest_enabled")
