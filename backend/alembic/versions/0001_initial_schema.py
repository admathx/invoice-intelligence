"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-18

The first schema, written out table by table: Phase 0's tables as amended
by the P0+P1 review (commit a91a168: timezone-aware timestamps, unique
canonical SKU names), which is when the first databases were built.

This migration used to call Base.metadata.create_all() on the *current*
models, which made every later migration fail on an empty database: step 1
already created the columns and tables they go on to add (0004's
tenants.inbox_address was the first to collide). Nothing had noticed because
the development database was only ever migrated forward, never from empty.
A migration has to describe a fixed point in time; this one now does, and
tests/test_migrations.py builds a database from empty on every run to keep
it that way.
"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The distributors extraction recognizes, as of Phase 0. Frozen here rather
# than imported, for the same reason as the tables: later edits to the list
# in app/models/distributor.py belong in migrations of their own.
SEED_SLUGS = ["sysco", "us_foods", "gordon", "pfg", "other"]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('canonical_skus',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('category', sa.String(), nullable=False),
    sa.Column('subcategory', sa.String(), nullable=True),
    sa.Column('base_uom', sa.Enum('lb', 'oz', 'gal', 'fl_oz', 'each', 'dozen', name='base_uom'), nullable=False),
    sa.Column('gtin', sa.String(), nullable=True),
    sa.Column('manufacturer', sa.String(), nullable=True),
    sa.Column('description_embedding', Vector(384), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('distributors',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('slug', sa.String(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('tenants',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('metro', sa.String(), nullable=False),
    sa.Column('volume_tier', sa.Enum('under_500k', 'tier_500k_1m', 'tier_1m_3m', 'over_3m', name='volume_tier'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('invoices',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('distributor_id', sa.UUID(), nullable=True),
    sa.Column('invoice_number', sa.String(), nullable=True),
    sa.Column('invoice_date', sa.Date(), nullable=True),
    sa.Column('delivery_date', sa.Date(), nullable=True),
    sa.Column('subtotal', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('tax', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('total', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('source', sa.Enum('upload', 'email', 'photo', name='invoice_source'), nullable=False),
    sa.Column('original_file_uri', sa.String(), nullable=False),
    sa.Column('status', sa.Enum('received', 'rendering', 'extracting', 'extracted', 'needs_review', 'confirmed', 'failed', name='invoice_status'), nullable=False),
    sa.Column('extraction_model', sa.String(), nullable=True),
    sa.Column('extraction_cost_usd', sa.Numeric(precision=10, scale=6), nullable=True),
    sa.Column('extracted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['distributor_id'], ['distributors.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_invoices_tenant_id'), 'invoices', ['tenant_id'], unique=False)
    op.create_table('price_alerts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('canonical_sku_id', sa.UUID(), nullable=False),
    sa.Column('alert_type', sa.Enum('creep', 'off_contract', 'above_peer', name='alert_type'), nullable=False),
    sa.Column('baseline_price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('current_price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('pct_change', sa.Numeric(precision=8, scale=4), nullable=False),
    sa.Column('window_start', sa.Date(), nullable=False),
    sa.Column('window_end', sa.Date(), nullable=False),
    sa.Column('peer_median', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('peer_percentile', sa.Numeric(precision=8, scale=4), nullable=True),
    sa.Column('status', sa.Enum('open', 'acknowledged', 'resolved', 'dismissed', name='alert_status'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['canonical_sku_id'], ['canonical_skus.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_price_alerts_tenant_id'), 'price_alerts', ['tenant_id'], unique=False)
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_index(op.f('ix_users_tenant_id'), 'users', ['tenant_id'], unique=False)
    op.create_table('invoice_line_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('invoice_id', sa.UUID(), nullable=False),
    sa.Column('line_number', sa.Integer(), nullable=False),
    sa.Column('raw_description', sa.String(), nullable=False),
    sa.Column('raw_sku', sa.String(), nullable=True),
    sa.Column('raw_pack_size', sa.String(), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('extended_price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('uom', sa.String(), nullable=False),
    sa.Column('canonical_sku_id', sa.UUID(), nullable=True),
    sa.Column('normalized_qty_base', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('normalized_unit_price', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('base_uom', sa.Enum('lb', 'oz', 'gal', 'fl_oz', 'each', 'dozen', name='base_uom'), nullable=True),
    sa.Column('extraction_confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('match_confidence', sa.Numeric(precision=4, scale=3), nullable=True),
    sa.Column('review_status', sa.Enum('auto', 'pending', 'confirmed', 'corrected', name='review_status'), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['canonical_sku_id'], ['canonical_skus.id'], ),
    sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_invoice_line_items_canonical_sku_id'), 'invoice_line_items', ['canonical_sku_id'], unique=False)
    op.create_index(op.f('ix_invoice_line_items_invoice_id'), 'invoice_line_items', ['invoice_id'], unique=False)
    op.create_index(op.f('ix_invoice_line_items_tenant_id'), 'invoice_line_items', ['tenant_id'], unique=False)
    op.create_table('sku_aliases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('canonical_sku_id', sa.UUID(), nullable=False),
    sa.Column('distributor_id', sa.UUID(), nullable=False),
    sa.Column('raw_description', sa.String(), nullable=False),
    sa.Column('raw_sku', sa.String(), nullable=True),
    sa.Column('pack_size', sa.String(), nullable=True),
    sa.Column('confirmed_by_user_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['canonical_sku_id'], ['canonical_skus.id'], ),
    sa.ForeignKeyConstraint(['confirmed_by_user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['distributor_id'], ['distributors.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sku_aliases_canonical_sku_id'), 'sku_aliases', ['canonical_sku_id'], unique=False)
    op.create_index(op.f('ix_sku_aliases_distributor_id'), 'sku_aliases', ['distributor_id'], unique=False)
    op.create_index('ix_sku_aliases_distributor_raw_sku', 'sku_aliases', ['distributor_id', 'raw_sku'], unique=False)
    op.create_table('price_observations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('tenant_id', sa.UUID(), nullable=False),
    sa.Column('canonical_sku_id', sa.UUID(), nullable=False),
    sa.Column('distributor_id', sa.UUID(), nullable=False),
    sa.Column('observed_on', sa.Date(), nullable=False),
    sa.Column('unit_price_base', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('metro', sa.String(), nullable=False),
    sa.Column('volume_tier', sa.Enum('under_500k', 'tier_500k_1m', 'tier_1m_3m', 'over_3m', name='volume_tier'), nullable=False),
    sa.Column('invoice_line_item_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['canonical_sku_id'], ['canonical_skus.id'], ),
    sa.ForeignKeyConstraint(['distributor_id'], ['distributors.id'], ),
    sa.ForeignKeyConstraint(['invoice_line_item_id'], ['invoice_line_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('invoice_line_item_id')
    )
    op.create_index('ix_price_observations_benchmark_cell', 'price_observations', ['canonical_sku_id', 'metro', 'volume_tier', 'observed_on'], unique=False)
    op.create_index(op.f('ix_price_observations_tenant_id'), 'price_observations', ['tenant_id'], unique=False)

    distributors = sa.table(
        "distributors",
        sa.column("id", sa.UUID()),
        sa.column("name", sa.String),
        sa.column("slug", sa.String),
    )
    op.bulk_insert(
        distributors,
        [{"id": uuid.uuid4(), "name": slug.replace("_", " ").title(), "slug": slug} for slug in SEED_SLUGS],
    )


def downgrade() -> None:
    for table in (
        "price_observations",
        "sku_aliases",
        "invoice_line_items",
        "users",
        "price_alerts",
        "invoices",
        "tenants",
        "distributors",
        "canonical_skus",
    ):
        op.drop_table(table)
    for enum in ("review_status", "alert_status", "alert_type", "invoice_status", "invoice_source", "volume_tier", "base_uom"):
        op.execute(f"DROP TYPE IF EXISTS {enum}")
