"""Distributors' websites

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-02

A price alert can now say the same product costs less at another
distributor (app/analytics/alternatives.py). For one the business doesn't
buy from yet, the next thing it needs is where to find them: the name
links to their site. Set for the four shared distributors; a vendor a
business added for itself has none.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

WEBSITES = {
    "sysco": "https://www.sysco.com",
    "us_foods": "https://www.usfoods.com",
    "gordon": "https://www.gfs.com",
    "pfg": "https://www.performancefoodservice.com",
}


def upgrade() -> None:
    op.add_column("distributors", sa.Column("website", sa.String(), nullable=True))
    distributors = sa.table("distributors", sa.column("slug", sa.String), sa.column("website", sa.String))
    for slug, website in WEBSITES.items():
        op.execute(distributors.update().where(distributors.c.slug == slug).values(website=website))


def downgrade() -> None:
    op.drop_column("distributors", "website")
