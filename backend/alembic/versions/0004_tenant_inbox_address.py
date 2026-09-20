"""tenants.inbox_address for email routing

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-20

SPEC.md §10 Phase 6: per-tenant address routing. Nullable (a tenant can
exist before an address is issued) and unique (it's the routing key — two
tenants sharing one would make inbound mail ambiguous, which the spec says
must quarantine rather than guess).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("inbox_address", sa.String(), nullable=True))
    op.create_index("ix_tenants_inbox_address", "tenants", ["inbox_address"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tenants_inbox_address", table_name="tenants")
    op.drop_column("tenants", "inbox_address")
