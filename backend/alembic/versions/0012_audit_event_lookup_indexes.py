"""index the audit trail's lookups inside details

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-27

Two queries look inside audit_events.details on every request that makes
them, and the table only grows:

- an invoice's history (GET /invoices/{id}/history, the History panel on
  every invoice page) finds its lines' events by details->>'invoice_id';
- sign-in throttling counts recent failures by details->>'email'.

Both are partial, so they cost nothing for the events that don't have the key.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_audit_events_details_invoice_id ON audit_events ((details ->> 'invoice_id')) "
        "WHERE details ? 'invoice_id'"
    )
    op.execute(
        "CREATE INDEX ix_audit_events_failed_login_email ON audit_events ((details ->> 'email'), occurred_at) "
        "WHERE action = 'auth.login_failed'"
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_failed_login_email", "audit_events")
    op.drop_index("ix_audit_events_details_invoice_id", "audit_events")
