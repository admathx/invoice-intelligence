"""canonical_skus.lb_per_gal: price liquids sold by weight per gallon

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30

The catalog prices Canola Oil and Ketchup per gallon; distributors sell fry
oil in 35 lb jugs and ketchup in #10 cans (net weight), so those lines could
never be matched or priced. A pounds-per-gallon figure on the few liquids
sold both ways converts one to the other. Values as in app/normalize/catalog.py.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LB_PER_GAL = {
    "Canola Oil": "7.7",
    "Olive Oil Extra Virgin": "7.6",
    "Olive Oil Pure": "7.6",
    "Vegetable Oil": "7.7",
    "Peanut Oil": "7.6",
    "Ketchup": "9.5",
}


def upgrade() -> None:
    op.add_column("canonical_skus", sa.Column("lb_per_gal", sa.Numeric(6, 3), nullable=True))
    for name, value in LB_PER_GAL.items():
        op.execute(sa.text("UPDATE canonical_skus SET lb_per_gal = CAST(:v AS numeric) WHERE name = :n").bindparams(v=value, n=name))


def downgrade() -> None:
    op.drop_column("canonical_skus", "lb_per_gal")
