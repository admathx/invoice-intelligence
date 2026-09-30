"""distributors: proper names for the seeded distributors

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-30

0001 named them by title-casing their slugs, so people saw "Us Foods" and
"Pfg" on invoices, exports and, now, price alerts ("... from Us Foods").
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NAMES = {
    "us_foods": ("Us Foods", "US Foods"),
    "pfg": ("Pfg", "Performance Foodservice"),
    "gordon": ("Gordon", "Gordon Food Service"),
}


def upgrade() -> None:
    # Only a name still as 0001 left it, in case someone has renamed one.
    for slug, (old, new) in NAMES.items():
        op.execute(f"UPDATE distributors SET name = '{new}' WHERE slug = '{slug}' AND name = '{old}'")


def downgrade() -> None:
    for slug, (old, new) in NAMES.items():
        op.execute(f"UPDATE distributors SET name = '{old}' WHERE slug = '{slug}' AND name = '{new}'")
