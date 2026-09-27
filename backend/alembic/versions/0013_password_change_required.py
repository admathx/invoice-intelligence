"""users.password_change_required

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-27

A password an operator generated (a new login, a reset) is one the operator
has seen. Until its owner replaces it, the API refuses everything but
changing it (app.auth.current_user).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("password_change_required", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("users", "password_change_required")
