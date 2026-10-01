"""Several invoices in one file; the restaurant an invoice is billed to

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-30

- invoices.split_from_id: one file can hold several invoices (a week's
  stack scanned together, photos of two invoices in one email). The first
  stays on the invoice that was added; each of the others becomes its own
  invoice, pointing back at it (app/splitting.py).
- invoices.printed_customer, billed_elsewhere: the restaurant named on the
  invoice, and whether that is some other restaurant, in which case it is
  held (app/billed_to.py).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("printed_customer", sa.String(), nullable=True))
    op.add_column(
        "invoices", sa.Column("billed_elsewhere", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("invoices", sa.Column("split_from_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_invoices_split_from_id", "invoices", "invoices", ["split_from_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_invoices_split_from_id", "invoices", ["split_from_id"])


def downgrade() -> None:
    op.drop_index("ix_invoices_split_from_id", table_name="invoices")
    op.drop_constraint("fk_invoices_split_from_id", "invoices", type_="foreignkey")
    op.drop_column("invoices", "split_from_id")
    op.drop_column("invoices", "billed_elsewhere")
    op.drop_column("invoices", "printed_customer")
