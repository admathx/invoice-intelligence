"""sku_aliases.source_invoice_line_item_id — which line a correction came from

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26

An alias recorded who made a correction (tenant_id) but not which line it was
made on. That made one mistake unrecoverable: when an invoice turns out to
have been attributed to the wrong distributor, the corrections a reviewer made
on its lines sit under the wrong distributor's item codes, and there was no
way to tell them apart from the same tenant's genuine corrections for that
distributor. Deleting by (tenant, distributor, item code) would take the real
ones too; leaving them meant a later real invoice from that distributor
auto-matched the misattributed product.

ON DELETE SET NULL: an alias is a statement about an item code, and it stays
true when the line it was first read from is removed from a review screen.
Existing rows get NULL; their provenance is unknown and they are left alone.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sku_aliases",
        sa.Column("source_invoice_line_item_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sku_aliases_source_invoice_line_item_id",
        "sku_aliases",
        "invoice_line_items",
        ["source_invoice_line_item_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_sku_aliases_source_invoice_line_item_id", "sku_aliases", ["source_invoice_line_item_id"])


def downgrade() -> None:
    op.drop_index("ix_sku_aliases_source_invoice_line_item_id", table_name="sku_aliases")
    op.drop_constraint("fk_sku_aliases_source_invoice_line_item_id", "sku_aliases", type_="foreignkey")
    op.drop_column("sku_aliases", "source_invoice_line_item_id")
