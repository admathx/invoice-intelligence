import logging
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

from app import audit
from app.analytics.price_creep import upsert_creep_alerts
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal, bind_tenant
from app.extract.client import AnthropicExtractorClient, ExtractionFailedError, get_extractor
from app.extract.confidence import assess_extraction
from app.ingest.render import render_pdf_to_pngs
from app.models import Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant, build_price_observation
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import InvoiceStatus, ReviewStatus
from app.normalize.matcher import apply_match, match_line_item
from app.storage import get_storage, read_uri, render_key, renders_prefix

logger = logging.getLogger(__name__)

extractor = get_extractor()

# Statuses that mean this job already ran to completion. A second run (an RQ
# retry, or the same id enqueued twice) must not insert a second copy of the
# invoice's line items.
_ALREADY_PROCESSED = {InvoiceStatus.extracted, InvoiceStatus.needs_review, InvoiceStatus.confirmed}


def _get_invoice_bypassing_tenant_scope(db, invoice_id: uuid.UUID, *, lock: bool = False) -> Invoice | None:
    """The worker looks up an invoice by opaque id with no tenant known yet — the
    one legitimate case for bypassing the tenant guard (app.db.TenantScoped).
    Callers must bind_tenant(db, ...) from the result before running any other
    query against a tenant-scoped table.
    """
    return db.get(Invoice, invoice_id, execution_options={TENANT_SCOPE_BYPASS: True}, with_for_update=lock)


def _clear_prior_attempt(db, invoice_id: uuid.UUID) -> None:
    """Remove what an earlier, interrupted run of this job may have left.

    A run that failed partway (or crashed after committing) can leave line
    items behind. Without this, retrying the job appended a second full set:
    duplicated review-queue items, and quantities double-counted in every
    negotiation sheet that sums them.
    """
    line_ids = select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice_id)
    db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
    db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id))


def process_invoice(invoice_id: str) -> None:
    """RQ job: render -> extract -> assess -> persist.

    Uses AnthropicExtractorClient when ANTHROPIC_API_KEY is set, otherwise the
    deterministic FakeExtractorClient (see app.extract.client.get_extractor).
    """
    db = SessionLocal()
    try:
        # Locked from here until the `rendering` commit below, which makes the
        # status check and the clear one step. The invoice review endpoints
        # lock the same row, so a person editing a failed invoice and a retry
        # of its extraction job can no longer interleave: either the edit lands
        # first (status becomes needs_review, which this skips) or the job does
        # (status becomes rendering, which the review screen refuses). Without
        # it, a retry that had already read `failed` went on to clear lines
        # someone had typed in by hand in the meantime.
        invoice = _get_invoice_bypassing_tenant_scope(db, uuid.UUID(invoice_id), lock=True)
        if invoice is None or invoice.status in _ALREADY_PROCESSED:
            db.close()
            return
        bind_tenant(db, invoice.tenant_id)
        _clear_prior_attempt(db, invoice.id)

        invoice.status = InvoiceStatus.rendering
        db.commit()

        # Rendered into a scratch directory for the extractor, then stored
        # for the review screen. Any pages an earlier attempt stored are
        # cleared first: a retry of a shorter re-upload mustn't show stale ones.
        storage = get_storage()
        storage.delete_prefix(renders_prefix(invoice.id))
        with tempfile.TemporaryDirectory(prefix=f"render-{invoice_id}-") as scratch:
            page_paths = render_pdf_to_pngs(read_uri(invoice.original_file_uri), Path(scratch))
            for page_path in page_paths:
                storage.put(render_key(invoice.id, page_path.name), page_path.read_bytes())

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

        # SPEC.md §5: arithmetic validation is the confidence signal, not the
        # model's own self-reported certainty. Assessed BEFORE any line is
        # persisted, because the verdict decides whether this invoice's prices
        # may feed analytics at all: build_price_observation refuses anything
        # that isn't `extracted`. Doing it after (as this used to) meant
        # auto-matched lines on an invoice with a misread price had already
        # written observations by the time the arithmetic caught the error.
        invoice.status = assess_extraction(extracted).status

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
            # distributor's catalog the item codes belong to. Extraction's
            # 'other' is stored as a real row but is not a catalog: matching
            # against it pooled item codes from every distributor that ever
            # reached 'other' into one alias namespace, so a Sysco code
            # corrected on one unattributed invoice auto-matched a different
            # US Foods product with the same code on the next. The invoice is
            # already held (assess_extraction flags 'other'), and choosing the
            # real distributor on the review screen re-matches every line.
            if distributor is not None and distributor.slug != UNRECOGNIZED_SLUG:
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
                apply_match(line_item, match)

            db.add(line_item)

            # SPEC.md §6's auto tier (>=0.92 confidence) means "resolved, no
            # human needed", so it feeds analytics immediately, exactly as
            # seed_corpus_pipeline.py does for the synthetic corpus. The
            # factory returns None for an invoice that failed arithmetic.
            if line_item.review_status == ReviewStatus.auto:
                observation = build_price_observation(line_item, invoice, tenant)
                if observation is not None:
                    db.add(observation)
                    wrote_any_observation = True

        audit.record(
            db,
            None,
            "invoice.extracted",
            "invoice",
            invoice.id,
            invoice.tenant_id,
            status=invoice.status,
            distributor=extracted.distributor,
            line_count=len(extracted.line_items),
            extraction_model=invoice.extraction_model,
            extraction_cost_usd=invoice.extraction_cost_usd,
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        invoice = _get_invoice_bypassing_tenant_scope(db, uuid.UUID(invoice_id))
        if invoice is not None:
            invoice.status = InvoiceStatus.failed
            if isinstance(exc, ExtractionFailedError):
                # Both attempts were billed; record them even though nothing
                # validated (see ExtractionFailedError).
                invoice.extraction_cost_usd = Decimal(str(exc.cost_usd))
            audit.record(
                db,
                None,
                "invoice.extraction_failed",
                "invoice",
                invoice.id,
                invoice.tenant_id,
                error=f"{type(exc).__name__}: {exc}"[:500],
            )
            db.commit()
        db.close()
        raise

    # Outside the failure handler on purpose. The invoice and its line items
    # are durably committed above; refreshing alerts is derived work. It used
    # to sit inside the try, so a transient error here flipped a committed,
    # live invoice to `failed` while its observations kept feeding analytics.
    try:
        if wrote_any_observation:
            upsert_creep_alerts(db, invoice.tenant_id)
    except Exception:
        db.rollback()
        logger.exception("creep alert refresh failed for invoice %s; alerts will refresh on the next write", invoice_id)
    finally:
        db.close()
