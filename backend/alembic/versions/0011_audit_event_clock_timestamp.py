"""audit events stamped when written, not when their transaction began

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27

now() in Postgres is the transaction's start time, so every event recorded
in one transaction (a login created and its locations granted, for one)
carried the identical timestamp and sorted arbitrarily among
themselves. clock_timestamp() is the moment of the insert, so the log reads
in the order things happened.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("audit_events", "occurred_at", server_default=sa.text("clock_timestamp()"))


def downgrade() -> None:
    op.alter_column("audit_events", "occurred_at", server_default=sa.text("now()"))
