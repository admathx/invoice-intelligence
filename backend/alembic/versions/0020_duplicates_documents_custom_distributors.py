"""Duplicate invoices, non-invoice documents, a business's own distributors,
and lines that aren't products

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30

From a realistic test set: the same invoice uploaded as a PDF, a copy and a
rescan was counted three times; statements and price lists were read as
invoices; invoices from a local produce or seafood vendor could never be
attributed, so none of their items were tracked; and fees and discounts sat
on Match items with nothing to match.

- invoices.file_sha256: the stored file's hash, so an identical file is
  refused at upload before it's paid for.
- invoices.duplicate_of_id: set when another invoice from the same
  distributor has the same number; the copy is held until a person decides.
- invoices.document_type: set when what was uploaded isn't an invoice
  (statement, price list, other); held, and nothing on it is used.
- invoices.printed_distributor: the distributor's name as printed, to offer
  when adding one and to recognize it next time.
- distributors.account_key: the business that added it (a tenant's
  account, or the tenant itself; see account_key_column). Null for the
  shared, seeded distributors.
- review_status 'not_product': a fee, surcharge or discount line.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("file_sha256", sa.String(), nullable=True))
    op.create_index("ix_invoices_tenant_file_sha256", "invoices", ["tenant_id", "file_sha256"])
    op.add_column(
        "invoices",
        sa.Column(
            "duplicate_of_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True
        ),
    )
    op.add_column("invoices", sa.Column("document_type", sa.String(), nullable=True))
    op.add_column("invoices", sa.Column("printed_distributor", sa.String(), nullable=True))
    op.add_column("distributors", sa.Column("account_key", postgresql.UUID(as_uuid=True), nullable=True, index=True))
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE review_status ADD VALUE IF NOT EXISTS 'not_product'")


def downgrade() -> None:
    # Postgres can't drop an enum value; lines marked not_product go back to
    # waiting, which is what they were before.
    op.execute("UPDATE invoice_line_items SET review_status = 'pending' WHERE review_status = 'not_product'")
    op.drop_column("distributors", "account_key")
    op.drop_column("invoices", "printed_distributor")
    op.drop_column("invoices", "document_type")
    op.drop_column("invoices", "duplicate_of_id")
    op.drop_index("ix_invoices_tenant_file_sha256", table_name="invoices")
    op.drop_column("invoices", "file_sha256")
