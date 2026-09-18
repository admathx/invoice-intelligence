import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal, bind_tenant
from app.extract.client import FakeExtractorClient
from app.ingest.render import render_pdf_to_pngs
from app.models import Distributor, Invoice, InvoiceLineItem
from app.models.enums import InvoiceStatus

extractor = FakeExtractorClient()


def _get_invoice_bypassing_tenant_scope(db, invoice_id: uuid.UUID) -> Invoice | None:
    """The worker looks up an invoice by opaque id with no tenant known yet — the
    one legitimate case for bypassing the tenant guard (app.db.TenantScoped).
    Callers must bind_tenant(db, ...) from the result before running any other
    query against a tenant-scoped table.
    """
    return db.get(Invoice, invoice_id, execution_options={TENANT_SCOPE_BYPASS: True})


def process_invoice(invoice_id: str) -> None:
    """RQ job: render -> extract -> persist. Runs the Phase 0 fake extractor."""
    db = SessionLocal()
    try:
        invoice = _get_invoice_bypassing_tenant_scope(db, uuid.UUID(invoice_id))
        if invoice is None:
            return
        bind_tenant(db, invoice.tenant_id)

        invoice.status = InvoiceStatus.rendering
        db.commit()

        render_dir = Path(settings.upload_dir) / "renders" / invoice_id
        page_paths = render_pdf_to_pngs(invoice.original_file_uri, render_dir)

        invoice.status = InvoiceStatus.extracting
        db.commit()

        extracted, cost_usd = extractor.extract(page_paths)

        distributor = db.scalar(select(Distributor).where(Distributor.slug == extracted.distributor))

        invoice.distributor_id = distributor.id if distributor else None
        invoice.invoice_number = extracted.invoice_number
        invoice.invoice_date = datetime.strptime(extracted.invoice_date, "%Y-%m-%d").date()
        invoice.delivery_date = (
            datetime.strptime(extracted.delivery_date, "%Y-%m-%d").date() if extracted.delivery_date else None
        )
        # Decimal(...) raises InvalidOperation on anything unparseable, which the
        # except block below catches and routes the whole invoice to `failed` —
        # per SPEC.md §1, "wrong numbers are worse than missing numbers," so an
        # extractor returning a garbled number must not silently become a $0.00
        # line item on an invoice that still ships as `extracted`.
        invoice.subtotal = Decimal(extracted.subtotal)
        invoice.tax = Decimal(extracted.tax)
        invoice.total = Decimal(extracted.total)
        invoice.extraction_model = "fake-phase0"
        invoice.extraction_cost_usd = Decimal(str(cost_usd))
        invoice.extracted_at = datetime.now(timezone.utc)

        for line in extracted.line_items:
            db.add(
                InvoiceLineItem(
                    tenant_id=invoice.tenant_id,
                    invoice_id=invoice.id,
                    line_number=line.line_number,
                    raw_description=line.raw_description,
                    raw_sku=line.raw_sku,
                    raw_pack_size=line.raw_pack_size,
                    quantity=Decimal(line.quantity),
                    unit_price=Decimal(line.unit_price),
                    extended_price=Decimal(line.extended_price),
                    uom=line.uom,
                    extraction_confidence=Decimal(str(line.confidence)),
                )
            )

        invoice.status = InvoiceStatus.extracted
        db.commit()
    except Exception:
        db.rollback()
        invoice = _get_invoice_bypassing_tenant_scope(db, uuid.UUID(invoice_id))
        if invoice is not None:
            invoice.status = InvoiceStatus.failed
            db.commit()
        raise
    finally:
        db.close()
