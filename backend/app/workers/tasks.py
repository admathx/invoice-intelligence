import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.extract.client import FakeExtractorClient
from app.ingest.render import render_pdf_to_pngs
from app.models import Distributor, Invoice, InvoiceLineItem
from app.models.enums import InvoiceStatus

extractor = FakeExtractorClient()


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        return Decimal("0")


def process_invoice(invoice_id: str) -> None:
    """RQ job: render -> extract -> persist. Runs the Phase 0 fake extractor."""
    db = SessionLocal()
    try:
        invoice = db.get(Invoice, uuid.UUID(invoice_id))
        if invoice is None:
            return

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
        invoice.subtotal = _to_decimal(extracted.subtotal)
        invoice.tax = _to_decimal(extracted.tax)
        invoice.total = _to_decimal(extracted.total)
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
                    quantity=_to_decimal(line.quantity),
                    unit_price=_to_decimal(line.unit_price),
                    extended_price=_to_decimal(line.extended_price),
                    uom=line.uom,
                    extraction_confidence=Decimal(str(line.confidence)),
                )
            )

        invoice.status = InvoiceStatus.extracted
        db.commit()
    except Exception:
        db.rollback()
        invoice = db.get(Invoice, uuid.UUID(invoice_id))
        if invoice is not None:
            invoice.status = InvoiceStatus.failed
            db.commit()
        raise
    finally:
        db.close()
