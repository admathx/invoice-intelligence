import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.analytics.price_creep import upsert_creep_alerts
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal, bind_tenant
from app.extract.client import AnthropicExtractorClient, get_extractor
from app.extract.confidence import assess_extraction
from app.ingest.render import render_pdf_to_pngs
from app.models import Distributor, Invoice, InvoiceLineItem, Tenant, build_price_observation
from app.models.enums import InvoiceStatus, ReviewStatus
from app.normalize.matcher import match_line_item

extractor = get_extractor()


def _get_invoice_bypassing_tenant_scope(db, invoice_id: uuid.UUID) -> Invoice | None:
    """The worker looks up an invoice by opaque id with no tenant known yet — the
    one legitimate case for bypassing the tenant guard (app.db.TenantScoped).
    Callers must bind_tenant(db, ...) from the result before running any other
    query against a tenant-scoped table.
    """
    return db.get(Invoice, invoice_id, execution_options={TENANT_SCOPE_BYPASS: True})


def process_invoice(invoice_id: str) -> None:
    """RQ job: render -> extract -> persist -> route by arithmetic confidence.

    Uses AnthropicExtractorClient when ANTHROPIC_API_KEY is set, otherwise the
    deterministic FakeExtractorClient (see app.extract.client.get_extractor).
    """
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
        invoice.extraction_model = (
            settings.extraction_model if isinstance(extractor, AnthropicExtractorClient) else "fake"
        )
        invoice.extraction_cost_usd = Decimal(str(cost_usd))
        invoice.extracted_at = datetime.now(timezone.utc)

        # Needed for the denormalized metro/volume_tier on any price
        # observation this invoice produces (see below).
        tenant = db.get(Tenant, invoice.tenant_id)
        wrote_any_observation = False

        for line in extracted.line_items:
            quantity = Decimal(line.quantity)
            unit_price = Decimal(line.unit_price)
            line_item = InvoiceLineItem(
                id=uuid.uuid4(),
                tenant_id=invoice.tenant_id,
                invoice_id=invoice.id,
                line_number=line.line_number,
                raw_description=line.raw_description,
                raw_sku=line.raw_sku,
                raw_pack_size=line.raw_pack_size,
                quantity=quantity,
                unit_price=unit_price,
                extended_price=Decimal(line.extended_price),
                uom=line.uom,
                extraction_confidence=Decimal(str(line.confidence)),
            )

            # SPEC.md §6 normalization — only possible once we know which
            # distributor's alias table to check; an unrecognized distributor
            # ('other') already routes the whole invoice to needs_review via
            # assess_extraction below, so leaving these fields unset here is
            # honest, not a gap.
            if invoice.distributor_id is not None:
                match = match_line_item(
                    db,
                    distributor_id=invoice.distributor_id,
                    raw_sku=line.raw_sku,
                    raw_description=line.raw_description,
                    raw_pack_size=line.raw_pack_size,
                    quantity=quantity,
                    unit_price=unit_price,
                    uom=line.uom,
                    tenant_id=invoice.tenant_id,
                )
                line_item.canonical_sku_id = match.canonical_sku_id
                line_item.match_confidence = match.match_confidence
                line_item.normalized_qty_base = match.normalized_qty_base
                line_item.normalized_unit_price = match.normalized_unit_price
                line_item.base_uom = match.base_uom
                line_item.review_status = match.review_status

            db.add(line_item)

            # SPEC.md §6's auto tier (>=0.92 confidence) means "resolved, no
            # human needed" — so it feeds analytics immediately, exactly as
            # seed_corpus_pipeline.py does for the synthetic corpus. Without
            # this, the ~99% of real lines that auto-match would never
            # produce a price_observations row at all (app/api/review.py only
            # ever acts on lines in the `pending` review band), leaving
            # price creep / benchmarks / negotiation sheets with no live
            # data source.
            if line_item.review_status == ReviewStatus.auto:
                observation = build_price_observation(line_item, invoice, tenant)
                if observation is not None:
                    db.add(observation)
                    wrote_any_observation = True

        # SPEC.md §5: arithmetic validation is the confidence signal, not the
        # model's own self-reported certainty. Any failure routes to needs_review
        # rather than extracted — never silently ships a wrong number.
        assessment = assess_extraction(extracted)
        invoice.status = assessment.status
        db.commit()

        # Only after the observations above are durably committed, so
        # detect_price_creep's fresh SELECT actually sees them.
        if wrote_any_observation:
            upsert_creep_alerts(db, invoice.tenant_id)
    except Exception:
        db.rollback()
        invoice = _get_invoice_bypassing_tenant_scope(db, uuid.UUID(invoice_id))
        if invoice is not None:
            invoice.status = InvoiceStatus.failed
            db.commit()
        raise
    finally:
        db.close()
