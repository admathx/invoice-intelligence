"""invoices.source_message_id for email intake idempotency

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-20

app/ingest/email_stub.py parsed the Message-ID and threw it away, so a
redelivered email — a mail provider retry, or an operator re-dropping a
quarantined message after fixing the tenant address — created a second set of
invoices for the same PDFs, which then double-counted into benchmark cells and
creep windows.

Indexed, not unique: one email carrying several invoice PDFs legitimately
produces several invoices sharing a Message-ID, so uniqueness lives at the
(tenant_id, source_message_id) *email* level, checked before ingest, rather
than as a row constraint.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("source_message_id", sa.String(), nullable=True))
    op.create_index("ix_invoices_source_message_id", "invoices", ["source_message_id"])


def downgrade() -> None:
    op.drop_index("ix_invoices_source_message_id", table_name="invoices")
    op.drop_column("invoices", "source_message_id")
