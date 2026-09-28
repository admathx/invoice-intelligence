"""price-increase emails: per-user opt-out, and a record of what was sent

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-28

alert_email_sends makes them idempotent: one row per alert per person,
unique, so a scheduler restart or a second scheduler can't email the same
increase twice.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("alert_emails_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_table(
        "alert_email_sends",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "alert_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("price_alerts.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("alert_id", "user_id", name="uq_alert_email_sends_alert_user"),
    )
    op.create_index("ix_alert_email_sends_user_id", "alert_email_sends", ["user_id"])


def downgrade() -> None:
    op.drop_table("alert_email_sends")
    op.drop_column("users", "alert_emails_enabled")
