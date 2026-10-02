"""Invoices typed in by hand

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-02

Every invoice began as a file: an upload, photos, or an email. A purchase
with no file to send (a paper invoice gone missing, a cash-and-carry run
written on a pad, a delivery only the distributor's website shows) couldn't
be recorded at all, so its spending and its prices were simply absent. A
person can now type one in: `typed` is where such an invoice came from.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE invoice_source ADD VALUE IF NOT EXISTS 'typed'")


def downgrade() -> None:
    # Postgres can't drop an enum value; typed invoices are kept, as uploads.
    op.execute("UPDATE invoices SET source = 'upload' WHERE source = 'typed'")
