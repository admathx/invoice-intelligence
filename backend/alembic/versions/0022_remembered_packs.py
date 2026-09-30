"""remembered_packs: a pack size a person entered, used for the item from then on

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-30

Many invoices print no pack size (or one the app can't read), so those items
could never be priced. A person enters it once; it is remembered per
business, distributor and item (its item code, or its description when there
is none), applied to that item's earlier lines, and used on every later
invoice. invoice_line_items.pack_size_remembered marks a pack that came from
here rather than from the page.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "remembered_packs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("distributor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("distributors.id"), nullable=False),
        sa.Column("item_key", sa.String(), nullable=False),
        sa.Column("pack_size", sa.String(), nullable=False),
        sa.Column("set_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_key", "distributor_id", "item_key", name="uq_remembered_packs_item"),
    )
    op.add_column(
        "invoice_line_items",
        sa.Column("pack_size_remembered", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("invoice_line_items", "pack_size_remembered")
    op.drop_table("remembered_packs")
