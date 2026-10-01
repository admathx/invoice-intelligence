"""An invoice sharing its page with another; unit prices to six places

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-01

- invoices.shares_page: the page also shows another, separate invoice (two
  half-page tickets copied onto one sheet). The first is read and kept for
  a person, who is told to add the other.
- Prices per base unit go from four decimal places to six. A case of 3,000
  napkins at $37.43 is $0.012477 each: at four places a 5% increase could
  round to 4% or 6%, and a four-cent case of 1,000 came to $0.0000 each.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UNIT_PRICES = (
    ("invoice_line_items", "normalized_unit_price"),
    ("price_observations", "unit_price_base"),
    ("price_alerts", "baseline_price"),
    ("price_alerts", "current_price"),
    ("price_alerts", "peer_median"),
)


def upgrade() -> None:
    op.add_column("invoices", sa.Column("shares_page", sa.Boolean(), nullable=False, server_default=sa.false()))
    for table, column in _UNIT_PRICES:
        op.alter_column(table, column, type_=sa.Numeric(14, 6), existing_type=sa.Numeric(12, 4))


def downgrade() -> None:
    for table, column in _UNIT_PRICES:
        op.alter_column(table, column, type_=sa.Numeric(12, 4), existing_type=sa.Numeric(14, 6))
    op.drop_column("invoices", "shares_page")
