"""accounts, and tenants.account_id

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-20

A tenant is a location; a multi-unit operator is several tenants. SPEC.md §7
suppresses a benchmark cell below 5 *distinct tenants*, which a five-location
group clears on its own — so the cell it gets back compares the group to
itself. accounts is the grouping key that lets benchmarking count distinct
businesses instead (app/analytics/benchmark.py).

account_id is nullable on purpose rather than backfilled to a per-tenant
account row: NULL means "its own business," which is what every pre-existing
tenant is, so this migration changes no existing benchmark result.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("tenants", sa.Column("account_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_tenants_account_id", "tenants", "accounts", ["account_id"], ["id"])
    op.create_index("ix_tenants_account_id", "tenants", ["account_id"])


def downgrade() -> None:
    op.drop_index("ix_tenants_account_id", table_name="tenants")
    op.drop_constraint("fk_tenants_account_id", "tenants", type_="foreignkey")
    op.drop_column("tenants", "account_id")
    op.drop_table("accounts")
