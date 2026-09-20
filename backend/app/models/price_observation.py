from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Date, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.enums import VolumeTier

if TYPE_CHECKING:
    from app.models.invoice import Invoice
    from app.models.invoice_line_item import InvoiceLineItem
    from app.models.tenant import Tenant


class PriceObservation(Base):
    """Denormalized read model for benchmarking. Written after a line item is confirmed.

    Deliberately NOT TenantScoped (app.db.TenantScoped), unlike the other tables
    with a tenant_id column: SPEC.md §7 benchmarking reads across every tenant in
    a (metro, volume_tier) cell by design (with suppression below 5 distinct
    tenants, not tenant-id filtering) — that's this table's whole purpose. Making
    it TenantScoped would force every Phase 4 benchmark query to carry a
    tenant_scope_bypass execution option, which would just be noise rather than
    a meaningful safety check for this one table.
    """

    __tablename__ = "price_observations"
    __table_args__ = (
        Index(
            "ix_price_observations_benchmark_cell",
            "canonical_sku_id",
            "metro",
            "volume_tier",
            "observed_on",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    canonical_sku_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canonical_skus.id"), nullable=False
    )
    distributor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("distributors.id"), nullable=False
    )

    observed_on: Mapped[date] = mapped_column(Date, nullable=False)
    unit_price_base: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)

    metro: Mapped[str] = mapped_column(String, nullable=False)
    volume_tier: Mapped[VolumeTier] = mapped_column(Enum(VolumeTier, name="volume_tier"), nullable=False)

    invoice_line_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invoice_line_items.id"), nullable=False, unique=True
    )


def build_price_observation(
    line: "InvoiceLineItem", invoice: "Invoice", tenant: "Tenant"
) -> PriceObservation | None:
    """The one place "a line item resolved to a comparable price" turns into
    a PriceObservation row — shared by app/api/review.py's confirm/correct
    (a human resolving a line) and backend/scripts/seed_corpus_pipeline.py
    (ground truth standing in for that same resolution at corpus scale).
    Previously implemented twice, independently, with no shared source of
    truth — a code-review finding on Phase 5.

    Returns None when the line isn't actually resolvable to a comparable
    price yet (no canonical_sku_id, no normalized price, or the invoice
    itself is missing a date/distributor).
    """
    if line.canonical_sku_id is None or line.normalized_unit_price is None:
        return None
    if invoice.invoice_date is None or invoice.distributor_id is None:
        return None
    return PriceObservation(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        canonical_sku_id=line.canonical_sku_id,
        distributor_id=invoice.distributor_id,
        observed_on=invoice.invoice_date,
        unit_price_base=line.normalized_unit_price,
        metro=tenant.metro,
        volume_tier=tenant.volume_tier,
        invoice_line_item_id=line.id,
    )
