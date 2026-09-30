"""distributors.name_key: find a distributor by name, once per business

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-30

The name reduced to what identifies it (app/business_distributors.py
name_key), stored so recognizing a vendor by the name printed on its invoice
is one indexed lookup rather than a scan of every distributor, and unique
within a business so two people adding the same vendor at once can't create
it twice.
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# As app/business_distributors.py name_key when this was written (a
# migration doesn't import app code, which may change after it).
_NOISE = re.compile(r"\b(INC|INCORPORATED|LLC|LTD|CO|COMPANY|CORP|CORPORATION|THE|AND)\b")


def _name_key(name: str) -> str:
    words = _NOISE.sub(" ", re.sub(r"[^A-Z0-9 ]+", " ", name.upper()))
    return "".join(words.split())


def upgrade() -> None:
    op.add_column("distributors", sa.Column("name_key", sa.String(), nullable=True))
    conn = op.get_bind()
    for distributor_id, name in conn.execute(sa.text("SELECT id, name FROM distributors")).all():
        conn.execute(
            sa.text("UPDATE distributors SET name_key = :k WHERE id = :i"), {"k": _name_key(name), "i": distributor_id}
        )
    op.create_index("ix_distributors_name_key", "distributors", ["name_key"])
    op.create_index(
        "ix_distributors_business_name_unique",
        "distributors",
        ["account_key", "name_key"],
        unique=True,
        postgresql_where=sa.text("account_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_distributors_business_name_unique", table_name="distributors")
    op.drop_index("ix_distributors_name_key", table_name="distributors")
    op.drop_column("distributors", "name_key")
