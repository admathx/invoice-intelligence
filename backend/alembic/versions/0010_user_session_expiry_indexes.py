"""indexes for pruning dead sessions

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27

app.auth.prune_sessions deletes sessions that expired or were revoked more
than session_retention_days ago, on every sign-in. Without these it would
scan the whole table each time.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_revoked_at", "user_sessions", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_user_sessions_revoked_at", "user_sessions")
    op.drop_index("ix_user_sessions_expires_at", "user_sessions")
