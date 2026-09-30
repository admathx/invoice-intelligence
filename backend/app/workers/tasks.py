import decimal
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

from app import audit, business_distributors, ops, packs
from app.analytics.price_creep import upsert_creep_alerts
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal, bind_tenant
from app.extract.client import AnthropicExtractorClient, ExtractionFailedError, get_extractor
from app.duplicates import find_original
from app.extract.charges import is_charge
from app.extract.confidence import assess_extraction
from app.extract.schema import DOCUMENT_TYPES, PRICED_DOCUMENT_TYPES
from app.extract.amounts import parse_amount
from app.extract.dates import parse_invoice_date
from app.extract.units import billing_unit
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

# Failures that are about the invoice itself (couldn't be read twice, or read
# as nonsense numbers or dates), not about the service. ValueError covers
# pydantic's validation errors and unparseable dates.
_INVOICE_PROBLEMS = (ExtractionFailedError, decimal.InvalidOperation, ValueError)


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
        invoice.printed_distributor = (extracted.distributor_name or "").strip() or None
        if distributor is None or distributor.slug == UNRECOGNIZED_SLUG:
            # A local vendor this business added before, recognized by the
            # name printed on it (app/business_distributors.py).
            distributor = business_distributors.find_by_name(db, invoice.tenant_id, invoice.printed_distributor) or distributor
        distributor_known = distributor is not None and distributor.slug != UNRECOGNIZED_SLUG

        invoice.distributor_id = distributor.id if distributor else None
        invoice.invoice_number = extracted.invoice_number
        # Whatever format the invoice prints it in (app/extract/dates.py). A
        # missing or unreadable date holds the invoice for a person (below)
        # rather than failing it: everything else on it may be fine.
        invoice.invoice_date = parse_invoice_date(extracted.invoice_date)
        invoice.delivery_date = parse_invoice_date(extracted.delivery_date)
        # Amounts as printed ("$1,234.50", "(12.50)": app/extract/amounts.py).
        # A blank or unreadable one stays empty, and the arithmetic check
        # below then holds the invoice for a person: per SPEC.md §1, "wrong
        # numbers are worse than missing numbers", so it never becomes a
        # guessed figure on an invoice that ships as `extracted`.
        invoice.subtotal = parse_amount(extracted.subtotal)
        invoice.tax = parse_amount(extracted.tax)
        invoice.total = parse_amount(extracted.total)
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
        invoice.status = assess_extraction(extracted, distributor_known=distributor_known).status
        if invoice.invoice_date is None:
            # Its prices can't be placed in time until someone adds the date
            # (the review screen asks for it).
            invoice.status = InvoiceStatus.needs_review

        # Held, whatever its numbers say, when it isn't an invoice (a
        # statement, a price list) or looks like a copy of one already added
        # (app/duplicates.py). Its lines are kept, unmatched and off Match
        # items, until a person deletes it or says otherwise.
        # Anything unrecognized is read as an invoice: holding a real one as
        # "not an invoice" hides its prices, which is worse than a statement
        # held for its numbers not adding up.
        kind = (extracted.document_type or "invoice").strip().lower()
        invoice.document_type = kind if kind in DOCUMENT_TYPES and kind not in PRICED_DOCUMENT_TYPES else None
        original = find_original(db, invoice)
        invoice.duplicate_of_id = original.id if original is not None else None
        held = invoice.document_type is not None or original is not None
        if held:
            invoice.status = InvoiceStatus.needs_review

        # Needed for the denormalized metro/volume_tier on any price
        # observation this invoice produces (see below).
        tenant = db.get(Tenant, invoice.tenant_id)
        wrote_any_observation = False
        # Packs a person entered for this distributor's items that print none
        # (app/packs.py), filled in below.
        remembered_packs = packs.remembered(db, invoice.tenant_id, distributor.id) if distributor_known else {}

        for line in extracted.line_items:
            # A line's numbers can't be stored empty; an unreadable one is
            # kept as 0, which fails the line's arithmetic, so it's
            # highlighted on the review screen for a person to fill in. The
            # invoice is already held: assess_extraction failed that line.
            quantity = parse_amount(line.quantity) or Decimal(0)
            unit_price = parse_amount(line.unit_price) or Decimal(0)
            extended_price = parse_amount(line.extended_price) or Decimal(0)
            uom = billing_unit(line.uom, line.raw_pack_size, quantity)
            raw_pack_size, pack_remembered = line.raw_pack_size, False
            remembered_pack = remembered_packs.get(packs.item_key(line.raw_sku, line.raw_description))
            if remembered_pack and packs.needs_pack(line.raw_pack_size, uom, line.raw_description):
                raw_pack_size, pack_remembered = remembered_pack, True
            line_item = InvoiceLineItem(
                id=uuid.uuid4(),
                tenant_id=invoice.tenant_id,
                invoice_id=invoice.id,
                line_number=line.line_number,
                raw_description=line.raw_description,
                raw_sku=line.raw_sku,
                raw_pack_size=raw_pack_size,
                pack_size_remembered=pack_remembered,
                quantity=quantity,
                unit_price=unit_price,
                extended_price=extended_price,
                uom=uom,
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
            if is_charge(line.raw_description, line.raw_pack_size):
                # A fee or discount: nothing to match (app/extract/charges.py).
                line_item.review_status = ReviewStatus.not_product
            elif distributor_known and not held:
                match = match_line_item(
                    db,
                    distributor_id=invoice.distributor_id,
                    raw_sku=line.raw_sku,
                    raw_description=line.raw_description,
                    raw_pack_size=raw_pack_size,
                    quantity=quantity,
                    unit_price=unit_price,
                    uom=uom,
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
            printed_distributor=invoice.printed_distributor,
            document_type=invoice.document_type,
            duplicate_of=invoice.duplicate_of_id,
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
        # An unreadable invoice is the invoice's problem, and it's already
        # marked for a person to type in. Anything else (the API key, the
        # credit balance, the model service, storage, the database) stops
        # every invoice, not just this one: someone needs to know.
        if not isinstance(exc, _INVOICE_PROBLEMS):
            ops.alert(
                f"worker:{type(exc).__name__}",
                f"Invoices aren't being read: {type(exc).__name__}",
                f"Invoice {invoice_id} failed, and others likely will until this is fixed.",
                exc=exc,
            )
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
