"""Reviewing an invoice whose numbers failed the arithmetic check.

An invoice lands in `needs_review` when its extracted numbers don't add up
(SPEC.md §5), and until this module existed that was a dead end. Its prices are
rightly kept out of analytics (build_price_observation refuses anything not
`extracted`), but nothing let a person fix the misread digit and let the
invoice back in. The line-level review queue settles *which SKU* a line is, not
whether its numbers were read correctly, and `InvoiceStatus.confirmed` existed
in the schema without anything ever setting it. So a real invoice with one
OCR error silently dropped out of every benchmark, creep alert and negotiation
sheet for good.

The flow: a person reads the rendered page beside the extracted numbers,
corrects what was misread (PATCH), and confirms (POST .../confirm) once the
same arithmetic rules the worker applies now pass. Confirming moves the
invoice to `confirmed`, which build_price_observation accepts, and writes the
observations its resolved lines would have written in the first place.

Only `needs_review` invoices can be edited. An `extracted` invoice's numbers
already feed analytics, and rewriting them in place would silently change
benchmarks other tenants are looking at with no trace of why.
"""
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.analytics.price_creep import upsert_creep_alerts
from app.api.deps import get_tenant_or_404
from app.config import settings
from app.db import get_db_for_tenant
from app.extract.confidence import check_arithmetic
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant, build_price_observation
from app.models.enums import InvoiceStatus, ReviewStatus
from app.normalize.matcher import match_line_item, normalize_price
from app.schemas.invoices import (
    InvoiceCheckOut,
    InvoiceDetailOut,
    InvoiceEdit,
    InvoiceOut,
    LineItemCreate,
    LineItemOut,
    LineItemSuggestion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoice review"])

# Line states whose identity is settled, so their price can feed analytics once
# the invoice's numbers are trusted. A `pending` line still waits on the line
# queue, and gets its observation when that resolves it (review.py _finalize).
_RESOLVED = {ReviewStatus.auto, ReviewStatus.confirmed, ReviewStatus.corrected}


@dataclass
class InvoiceCheck:
    failed_line_numbers: list[int] = field(default_factory=list)
    lines_sum_to_subtotal: bool = True
    totals_reconcile: bool = True
    missing_distributor: bool = False
    missing_invoice_date: bool = False
    no_line_items: bool = False

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.no_line_items:
            out.append("no line items")
        if self.failed_line_numbers:
            out.append(f"quantity x unit price doesn't equal the extended price on line(s) {self.failed_line_numbers}")
        if self.no_line_items:
            # With nothing to add up, "lines don't sum to the subtotal" is
            # noise on top of the one thing to do: enter the lines.
            pass
        elif not self.lines_sum_to_subtotal:
            out.append("line totals don't add up to the subtotal")
        elif not self.totals_reconcile:
            # Only reported once the lines reconcile: until then the subtotal
            # itself is suspect, so this would be noise on top of the real error.
            out.append("subtotal + tax doesn't equal the total")
        if self.missing_distributor:
            out.append("distributor not recognized — choose one")
        if self.missing_invoice_date:
            out.append("invoice date missing")
        return out

    @property
    def passes(self) -> bool:
        return not self.reasons


def check_stored_invoice(
    invoice: Invoice, lines: list[InvoiceLineItem], distributor: Distributor | None
) -> InvoiceCheck:
    """The worker's arithmetic rules (check_arithmetic), applied to the stored,
    possibly human-corrected numbers.

    Deliberately omits the extractor's per-line self-confidence, which the
    worker's assess_extraction also counts: that is the model's guess about
    its own reading, and a person looking at the page is exactly what replaces
    it. SPEC.md §5 is explicit that arithmetic, not self-reported confidence,
    is the signal that works.
    """
    arithmetic = check_arithmetic(
        [(line.line_number, line.quantity, line.unit_price, line.extended_price) for line in lines],
        invoice.subtotal,
        invoice.tax,
        invoice.total,
    )
    return InvoiceCheck(
        failed_line_numbers=arithmetic.failed_line_numbers,
        lines_sum_to_subtotal=arithmetic.lines_sum_to_subtotal,
        totals_reconcile=arithmetic.totals_reconcile,
        # A distributor is required, not optional, for the same reason the
        # worker flags 'other': line identity comes from that distributor's
        # item codes. And 'other' counts as missing, not present. The worker
        # stores it as a real row, so checking for NULL alone let an invoice
        # extraction couldn't attribute be confirmed without anyone choosing.
        missing_distributor=distributor is None or distributor.slug == UNRECOGNIZED_SLUG,
        missing_invoice_date=invoice.invoice_date is None,
        no_line_items=not lines,
    )


def _distributor(db: Session, invoice: Invoice) -> Distributor | None:
    return db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None


def _lines(db: Session, invoice_id: uuid.UUID) -> list[InvoiceLineItem]:
    return list(
        db.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == invoice_id)
            .order_by(InvoiceLineItem.line_number)
        )
    )


def build_invoice_detail(db: Session, invoice: Invoice) -> InvoiceDetailOut:
    """Shared by GET /invoices/{id} and both review endpoints, so the screen
    always gets back the same shape, including the re-run check."""
    lines = _lines(db, invoice.id)
    distributor = _distributor(db, invoice)
    check = check_stored_invoice(invoice, lines, distributor)

    render_dir = Path(settings.upload_dir) / "renders" / str(invoice.id)
    page_image_urls = (
        [f"/renders/{invoice.id}/{p.name}" for p in sorted(render_dir.glob("page_*.png"))]
        if render_dir.is_dir()
        else []
    )
    return InvoiceDetailOut(
        **InvoiceOut.model_validate(invoice).model_dump(),
        distributor_name=distributor.name if distributor else None,
        line_items=[LineItemOut.model_validate(line) for line in lines],
        page_image_urls=page_image_urls,
        check=InvoiceCheckOut(
            passes=check.passes, reasons=check.reasons, failed_line_numbers=check.failed_line_numbers
        ),
    )


def _match(db: Session, invoice: Invoice, line: InvoiceLineItem) -> None:
    """Run the normal matcher on a line and apply the result — the same path
    an extracted line takes, so a hand-entered or re-attributed line gets no
    special treatment. Without a recognized distributor there is no catalog to
    match against; the line waits as pending until one is chosen, and choosing
    one re-matches every line (edit_invoice)."""
    distributor = _distributor(db, invoice)
    if distributor is None or distributor.slug == UNRECOGNIZED_SLUG:
        line.canonical_sku_id = line.match_confidence = line.base_uom = None
        line.normalized_qty_base, line.normalized_unit_price = normalize_price(
            line.raw_pack_size, line.quantity, line.unit_price, line.uom
        )
        line.review_status = ReviewStatus.pending
        return
    match = match_line_item(
        db,
        distributor_id=distributor.id,
        raw_sku=line.raw_sku,
        raw_description=line.raw_description,
        raw_pack_size=line.raw_pack_size,
        quantity=line.quantity,
        unit_price=line.unit_price,
        uom=line.uom,
        tenant_id=invoice.tenant_id,
    )
    line.canonical_sku_id = match.canonical_sku_id
    line.match_confidence = match.match_confidence
    line.normalized_qty_base = match.normalized_qty_base
    line.normalized_unit_price = match.normalized_unit_price
    line.base_uom = match.base_uom
    line.review_status = match.review_status


def _get_reviewable_invoice(db: Session, invoice_id: uuid.UUID, *, editing: bool) -> Invoice:
    """The invoice, locked, if a person may work on it.

    Editing also accepts `failed`: extraction produced nothing usable, and
    entering the lines by hand from the page image is the way to recover it.
    Confirming doesn't; a failed invoice is confirmed through needs_review
    like any other (see _take_under_review).
    """
    # FOR UPDATE: two people confirming the same invoice at once would
    # otherwise both see needs_review and both write its observations.
    invoice = db.get(Invoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    allowed = {InvoiceStatus.needs_review, InvoiceStatus.failed} if editing else {InvoiceStatus.needs_review}
    if invoice.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"invoice is {invoice.status.value}; only invoices that need review can be edited or confirmed",
        )
    return invoice


def _take_under_review(invoice: Invoice) -> None:
    """A failed invoice becomes needs_review the moment a person edits it.

    Not just a label: the worker skips needs_review invoices, but retries
    `failed` ones, and a retry clears whatever lines an earlier attempt left
    before re-extracting (tasks._clear_prior_attempt). Left as `failed`, a
    re-enqueued job would delete lines someone had just typed in by hand.
    """
    if invoice.status == InvoiceStatus.failed:
        invoice.status = InvoiceStatus.needs_review


@router.patch("/{invoice_id}", response_model=InvoiceDetailOut)
def edit_invoice(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, body: InvoiceEdit, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    lines = {line.id: line for line in _lines(db, invoice.id)}

    # All validation before any mutation, so a bad request changes nothing.
    unknown = [str(edit.id) for edit in body.line_items if edit.id not in lines]
    if unknown:
        raise HTTPException(status_code=422, detail=f"line items not on this invoice: {unknown}")
    sent = body.model_fields_set
    distributor_changed = (
        "distributor_id" in sent and body.distributor_id is not None and body.distributor_id != invoice.distributor_id
    )
    if distributor_changed:
        chosen = db.get(Distributor, body.distributor_id)
        if chosen is None or chosen.slug == UNRECOGNIZED_SLUG:
            raise HTTPException(status_code=422, detail="choose a recognized distributor")

    for name in ("invoice_date", "subtotal", "tax", "total"):
        if name in sent and getattr(body, name) is not None:
            setattr(invoice, name, getattr(body, name))

    for edit in body.line_items:
        line = lines[edit.id]
        repriced = False
        for name in ("quantity", "unit_price", "extended_price"):
            value = getattr(edit, name)
            if name in edit.model_fields_set and value is not None:
                setattr(line, name, value)
                repriced = repriced or name != "extended_price"
        if repriced:
            # The line's identity doesn't depend on its price, so its match
            # stands; its price per base unit was computed from the misread
            # number and has to be redone from the corrected one.
            line.normalized_qty_base, line.normalized_unit_price = normalize_price(
                line.raw_pack_size, line.quantity, line.unit_price, line.uom
            )

    if distributor_changed:
        invoice.distributor_id = body.distributor_id
        # Item codes only mean something within one distributor's catalog, so
        # every match made under the old (or no) distributor is void, even a
        # line a person already confirmed: that confirmation was of a code
        # read against the wrong catalog.
        for line in lines.values():
            _match(db, invoice, line)

    _take_under_review(invoice)
    db.commit()
    return build_invoice_detail(db, invoice)


@router.post("/{invoice_id}/confirm", response_model=InvoiceDetailOut)
def confirm_invoice(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=False)
    lines = _lines(db, invoice.id)

    check = check_stored_invoice(invoice, lines, _distributor(db, invoice))
    if not check.passes:
        # The same rules the worker enforced: confirming is a claim that the
        # numbers are now right, so it can't be granted while they still
        # don't add up.
        raise HTTPException(status_code=422, detail={"message": "invoice still doesn't reconcile", "reasons": check.reasons})

    invoice.status = InvoiceStatus.confirmed
    tenant = db.get(Tenant, invoice.tenant_id)
    already_observed = set(
        db.scalars(
            select(PriceObservation.invoice_line_item_id).where(
                PriceObservation.invoice_line_item_id.in_([line.id for line in lines])
            )
        )
    )
    wrote_any_observation = False
    for line in lines:
        if line.review_status in _RESOLVED and line.id not in already_observed:
            observation = build_price_observation(line, invoice, tenant)
            if observation is not None:
                db.add(observation)
                wrote_any_observation = True
    db.commit()

    # Derived work after the durable commit, and non-fatal for the same reason
    # as in the worker: a failure here must not unwind a confirmation.
    if wrote_any_observation:
        try:
            upsert_creep_alerts(db, invoice.tenant_id)
        except Exception:
            db.rollback()
            logger.exception("creep alert refresh failed after confirming invoice %s", invoice_id)

    return build_invoice_detail(db, invoice)


@router.post("/{invoice_id}/line-items", response_model=InvoiceDetailOut, status_code=201)
def add_line_item(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, body: LineItemCreate, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    """Add a line a person read off the invoice image.

    The recovery path for an invoice where extraction found nothing (it lands
    in needs_review with "no line items", or in `failed`), and for a single
    line extraction skipped. Matched by the ordinary matcher, so a line that
    was picked from this tenant's past purchases resolves the same way it did
    then, through the tenant's own alias or the same embedding match.
    """
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    existing = _lines(db, invoice.id)

    line = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=invoice.tenant_id,
        invoice_id=invoice.id,
        # Appended after whatever is there. Never reuses a number: line
        # numbers are what the check's reasons and the screen refer to.
        line_number=max((existing_line.line_number for existing_line in existing), default=0) + 1,
        raw_description=body.raw_description.strip(),
        raw_sku=(body.raw_sku or "").strip() or None,
        raw_pack_size=(body.raw_pack_size or "").strip() or None,
        uom=body.uom.strip().upper(),
        quantity=body.quantity,
        unit_price=body.unit_price,
        extended_price=body.extended_price,
        # No extraction confidence: nothing was extracted. A person typed it.
        extraction_confidence=None,
    )
    _match(db, invoice, line)
    db.add(line)
    _take_under_review(invoice)
    db.commit()
    return build_invoice_detail(db, invoice)


@router.delete("/{invoice_id}/line-items/{line_item_id}", response_model=InvoiceDetailOut)
def remove_line_item(
    invoice_id: uuid.UUID, line_item_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    """Remove a line that isn't on the page: a mistyped manual entry, or one
    extraction hallucinated. Only on an invoice under review, whose lines by
    definition haven't fed analytics, so there's no observation to unwind."""
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    line = db.get(InvoiceLineItem, line_item_id)
    if line is None or line.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="line item not on this invoice")
    db.delete(line)
    _take_under_review(invoice)
    db.commit()
    return build_invoice_detail(db, invoice)


SUGGESTION_LIMIT = 10


@router.get("/{invoice_id}/line-item-suggestions", response_model=list[LineItemSuggestion])
def suggest_line_items(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, q: str = "", db: Session = Depends(get_db_for_tenant)
) -> list[LineItemSuggestion]:
    """Lines this tenant has bought before, to fill a hand-entered one from.

    Scoped to the invoice's distributor when it's recognized, since an item
    code means nothing outside its own catalog; across all of the tenant's
    distributors when it isn't. Drawn only from invoices whose numbers passed
    the check (`extracted` or `confirmed`), because the last price comes along
    as a hint, and a hint read off a misread invoice would mislead the person
    typing. One entry per item, its most recent purchase.
    """
    get_tenant_or_404(db, tenant_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")

    identity = func.coalesce(InvoiceLineItem.raw_sku, InvoiceLineItem.raw_description)
    conditions = [
        Invoice.status.in_([InvoiceStatus.extracted, InvoiceStatus.confirmed]),
        Invoice.id != invoice.id,
    ]
    distributor = _distributor(db, invoice)
    if distributor is not None and distributor.slug != UNRECOGNIZED_SLUG:
        conditions.append(Invoice.distributor_id == distributor.id)
    if q.strip():
        pattern = f"%{q.strip()}%"
        conditions.append(or_(InvoiceLineItem.raw_description.ilike(pattern), InvoiceLineItem.raw_sku.ilike(pattern)))

    # DISTINCT ON keeps each item's most recent purchase: identity first to
    # satisfy Postgres' ordering rule, then newest-first within it.
    latest = (
        select(
            InvoiceLineItem.raw_description,
            InvoiceLineItem.raw_sku,
            InvoiceLineItem.raw_pack_size,
            InvoiceLineItem.uom,
            InvoiceLineItem.unit_price,
            InvoiceLineItem.canonical_sku_id,
            Invoice.invoice_date,
        )
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(*conditions, Invoice.invoice_date.is_not(None))
        .distinct(identity, InvoiceLineItem.raw_pack_size, InvoiceLineItem.uom)
        .order_by(identity, InvoiceLineItem.raw_pack_size, InvoiceLineItem.uom, Invoice.invoice_date.desc())
    ).subquery()
    rows = db.execute(
        select(latest, CanonicalSku.name)
        .outerjoin(CanonicalSku, CanonicalSku.id == latest.c.canonical_sku_id)
        .order_by(latest.c.invoice_date.desc(), latest.c.raw_description)
        .limit(SUGGESTION_LIMIT)
    ).all()
    return [
        LineItemSuggestion(
            raw_description=row.raw_description,
            raw_sku=row.raw_sku,
            raw_pack_size=row.raw_pack_size,
            uom=row.uom,
            canonical_sku_name=row.name,
            last_unit_price=row.unit_price,
            last_seen=row.invoice_date,
        )
        for row in rows
    ]
