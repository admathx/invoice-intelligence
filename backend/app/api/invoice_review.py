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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.price_creep import upsert_creep_alerts
from app.api.deps import get_tenant_or_404
from app.config import settings
from app.db import get_db_for_tenant
from app.extract.confidence import check_arithmetic
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models import Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant, build_price_observation
from app.models.enums import InvoiceStatus, ReviewStatus
from app.normalize.matcher import match_line_item, normalize_price
from app.schemas.invoices import InvoiceCheckOut, InvoiceDetailOut, InvoiceEdit, InvoiceOut, LineItemOut

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
        if not self.lines_sum_to_subtotal:
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


def _get_reviewable_invoice(db: Session, invoice_id: uuid.UUID) -> Invoice:
    # FOR UPDATE: two people confirming the same invoice at once would
    # otherwise both see needs_review and both write its observations.
    invoice = db.get(Invoice, invoice_id, with_for_update=True)
    if invoice is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    if invoice.status != InvoiceStatus.needs_review:
        raise HTTPException(
            status_code=409,
            detail=f"invoice is {invoice.status.value}; only invoices that need review can be edited or confirmed",
        )
    return invoice


@router.patch("/{invoice_id}", response_model=InvoiceDetailOut)
def edit_invoice(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, body: InvoiceEdit, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id)
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
            match = match_line_item(
                db,
                distributor_id=invoice.distributor_id,
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

    db.commit()
    return build_invoice_detail(db, invoice)


@router.post("/{invoice_id}/confirm", response_model=InvoiceDetailOut)
def confirm_invoice(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id)
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
