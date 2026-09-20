"""sku_aliases.tenant_id, for cross-tenant alias confirmation

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-20

SPEC.md §12's first open question: "Should a corrected alias apply across all
tenants immediately, or only after N confirmations? (Leaning: cross-tenant
after 2 independent confirmations.)" It was never resolved — aliases applied
across every tenant immediately, off one person's click, so a single mistaken
correction silently rewrote matching for every other customer.

Recording who made each correction is what makes counting independent
confirmations possible. Nullable, and NULL keeps the old always-trusted
behaviour, which is what a system-curated alias (a catalog import rather than
one user's judgement) should have. Existing rows therefore keep matching
exactly as they did.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sku_aliases", sa.Column("tenant_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_sku_aliases_tenant_id", "sku_aliases", "tenants", ["tenant_id"], ["id"])
    op.create_index("ix_sku_aliases_tenant_id", "sku_aliases", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_sku_aliases_tenant_id", table_name="sku_aliases")
    op.drop_constraint("fk_sku_aliases_tenant_id", "sku_aliases", type_="foreignkey")
    op.drop_column("sku_aliases", "tenant_id")
