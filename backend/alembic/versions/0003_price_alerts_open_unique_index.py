"""price_alerts: one open alert per (tenant, sku, type)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-19

Backs upsert_creep_alerts' check-then-act with an actual DB constraint —
without this, two concurrent review actions for the same tenant (e.g. two
browser tabs) could both observe "no open alert for this SKU yet" and both
insert one, producing two open creep alerts for the same canonical_sku_id.
Partial (WHERE status = 'open') because closed/acknowledged/resolved alerts
for the same SKU are legitimate history, not duplicates.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_price_alerts_open_unique "
        "ON price_alerts (tenant_id, canonical_sku_id, alert_type) "
        "WHERE status = 'open'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_price_alerts_open_unique")
