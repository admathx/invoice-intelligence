"""Keep the printed pack when a remembered one replaces it; remember the
description a pack was entered for

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-30

- invoice_line_items.printed_pack_size: what the invoice printed ("2-5LB
  AVG"), kept when a pack a person entered (app/packs.py) replaces it in
  raw_pack_size, so the page and exports can still say what it said.
- remembered_packs.raw_description: the item's description when the pack
  was entered. A distributor reusing an item code for another product must
  not inherit the old product's pack; the description has to still look like
  the same item, as for remembered matches (MIN_ALIAS_DESCRIPTION_SIMILARITY).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoice_line_items", sa.Column("printed_pack_size", sa.String(), nullable=True))
    op.add_column("remembered_packs", sa.Column("raw_description", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("remembered_packs", "raw_description")
    op.drop_column("invoice_line_items", "printed_pack_size")
