"""price_alerts: one open alert per (tenant, sku, distributor, type)

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-30

Price increases are detected per distributor. Pooling every distributor's
prices for a product hid real increases (a location buying the same cups
from a cheaper second distributor pulled the recent median down) and could
invent them (buying once from a pricier one). Existing alerts keep a null
distributor; the next refresh for their location resolves them and opens
per-distributor ones where the increase still holds.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "price_alerts",
        sa.Column("distributor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("distributors.id"), nullable=True),
    )
    op.execute("DROP INDEX IF EXISTS ix_price_alerts_open_unique")
    op.execute(
        "CREATE UNIQUE INDEX ix_price_alerts_open_unique "
        "ON price_alerts (tenant_id, canonical_sku_id, distributor_id, alert_type) "
        "WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_price_alerts_open_unique")
    # Several distributors' open alerts for one product would break the
    # older one-per-product index: keep the newest open.
    op.execute(
        "UPDATE price_alerts SET status = 'resolved' WHERE status = 'open' AND id NOT IN ("
        "SELECT DISTINCT ON (tenant_id, canonical_sku_id, alert_type) id FROM price_alerts "
        "WHERE status = 'open' ORDER BY tenant_id, canonical_sku_id, alert_type, created_at DESC)"
    )
    op.drop_column("price_alerts", "distributor_id")
    op.execute(
        "CREATE UNIQUE INDEX ix_price_alerts_open_unique "
        "ON price_alerts (tenant_id, canonical_sku_id, alert_type) "
        "WHERE status = 'open'"
    )
