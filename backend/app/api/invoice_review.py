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

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app import audit, business_distributors
from app.analytics.price_creep import upsert_creep_alerts
from app.api.deps import get_tenant_or_404
from app.auth import current_user, get_db_for_tenant
from app.duplicates import BEING_READ, find_original
from app.extract.charges import is_charge
from app.extract.confidence import check_arithmetic
from app.storage import forget_original, get_storage, page_names, renders_prefix
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models import (
    CanonicalSku,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceObservation,
    SkuAlias,
    Tenant,
    User,
    build_price_observation,
)
from app.models.enums import InvoiceStatus, ReviewStatus
from app.normalize.matcher import _exact_match_result, apply_match, match_line_item, normalize_price
from app.schemas.invoices import (
    InvoiceCheckOut,
    InvoiceDetailOut,
    InvoiceEdit,
    InvoiceOut,
    LineItemCreate,
    LineItemEdit,
    LineItemOut,
    LineItemSuggestion,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invoices", tags=["invoice review"])

# Line states whose identity is settled, so their price can feed analytics once
# the invoice's numbers are trusted. A `pending` line still waits on the line
# queue, and gets its observation when that resolves it (review.py _finalize).
_RESOLVED = {ReviewStatus.auto, ReviewStatus.confirmed, ReviewStatus.corrected}

# What the audit trail records about an invoice and a line: the numbers and
# text a person can change here, so "what did it say before" is answerable.
_INVOICE_AUDITED = ("invoice_date", "subtotal", "tax", "total", "distributor_id")
_LINE_AUDITED = ("raw_description", "raw_sku", "raw_pack_size", "uom", "quantity", "unit_price", "extended_price")


def _snapshot(obj, fields: tuple[str, ...]) -> dict:
    return {name: getattr(obj, name) for name in fields}


@dataclass
class InvoiceCheck:
    failed_line_numbers: list[int] = field(default_factory=list)
    lines_sum_to_subtotal: bool = True
    totals_reconcile: bool = True
    missing_distributor: bool = False
    missing_invoice_date: bool = False
    no_line_items: bool = False
    # Held whatever the numbers say: likely a copy of another invoice, or not
    # an invoice at all. Each has to be settled (delete it, or say otherwise)
    # before it can be confirmed.
    copy_of: str | None = None
    not_an_invoice: str | None = None

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.copy_of:
            out.append(f"It looks like a copy of {self.copy_of}. Delete it, or tell us it's a different invoice.")
        if self.not_an_invoice:
            out.append(
                f"This looks like {self.not_an_invoice}, not an invoice, so nothing on it is used. "
                "Delete it, or tell us it is an invoice."
            )
        if self.no_line_items:
            out.append("There are no items yet.")
        if self.failed_line_numbers:
            out.append(_line_mismatch(self.failed_line_numbers))
        if self.no_line_items:
            # With nothing to add up, "lines don't sum to the subtotal" is
            # noise on top of the one thing to do: enter the lines.
            pass
        elif not self.lines_sum_to_subtotal:
            out.append("The items don't add up to the subtotal.")
        elif not self.totals_reconcile:
            # Only reported once the lines reconcile: until then the subtotal
            # itself is suspect, so this would be noise on top of the real error.
            out.append("Subtotal plus tax doesn't equal the total.")
        if self.missing_distributor:
            out.append("Choose the distributor.")
        if self.missing_invoice_date:
            out.append("Add the invoice date.")
        return out

    @property
    def passes(self) -> bool:
        return not self.reasons


def _line_mismatch(line_numbers: list[int]) -> str:
    items = ", ".join(str(n) for n in line_numbers)
    which = f"item {items}" if len(line_numbers) == 1 else f"items {items}"
    return f"On {which}, qty × price each doesn't equal the line total."


_DOCUMENT_LABEL = {"statement": "a statement", "price_list": "a price list or order guide"}


def invoice_label(invoice: Invoice) -> str:
    """How the screen names an invoice: "invoice 88214 from 2026-04-03"."""
    number = f"invoice {invoice.invoice_number}" if invoice.invoice_number else "an invoice"
    return f"{number} from {invoice.invoice_date}" if invoice.invoice_date else f"{number} you already added"


def check_stored_invoice(
    invoice: Invoice, lines: list[InvoiceLineItem], distributor: Distributor | None, original: Invoice | None = None
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
        copy_of=invoice_label(original) if original is not None else None,
        not_an_invoice=(
            _DOCUMENT_LABEL.get(invoice.document_type, "something else") if invoice.document_type else None
        ),
    )


def _distributor(db: Session, invoice: Invoice) -> Distributor | None:
    return db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None


def _original(db: Session, invoice: Invoice) -> Invoice | None:
    return db.get(Invoice, invoice.duplicate_of_id) if invoice.duplicate_of_id else None


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
    original = _original(db, invoice)
    check = check_stored_invoice(invoice, lines, distributor, original)

    page_image_urls = [
        f"/invoices/{invoice.id}/pages/{name}?tenant_id={invoice.tenant_id}" for name in page_names(invoice.id)
    ]
    sku_ids = {line.canonical_sku_id for line in lines if line.canonical_sku_id}
    product = (
        dict(db.execute(select(CanonicalSku.id, CanonicalSku.name).where(CanonicalSku.id.in_(sku_ids))).all())
        if sku_ids
        else {}
    )
    return InvoiceDetailOut(
        **InvoiceOut.model_validate(invoice).model_dump(),
        distributor_name=distributor.name if distributor else None,
        duplicate_of_label=invoice_label(original) if original is not None else None,
        line_items=[
            LineItemOut.model_validate(line).model_copy(update={"canonical_sku_name": product.get(line.canonical_sku_id)})
            for line in lines
        ],
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
    if is_charge(line.raw_description, line.raw_pack_size):
        # A fee or discount: nothing to match (app/extract/charges.py).
        line.canonical_sku_id = line.match_confidence = line.base_uom = None
        line.normalized_qty_base = line.normalized_unit_price = None
        line.review_status = ReviewStatus.not_product
        return
    distributor = _distributor(db, invoice)
    if distributor is None or distributor.slug == UNRECOGNIZED_SLUG:
        line.canonical_sku_id = line.match_confidence = line.base_uom = None
        line.normalized_qty_base, line.normalized_unit_price = normalize_price(
            line.raw_pack_size, line.quantity, line.unit_price, line.uom, raw_description=line.raw_description
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
    apply_match(line, match)


def _apply_line_edit(db: Session, invoice: Invoice, line: InvoiceLineItem, edit: LineItemEdit) -> None:
    """Apply one line's corrections, then redo only what they invalidate.

    - Item code or description changed: the line may be a different product.
      Re-match it, and withdraw any correction a reviewer made on the misread
      code (by provenance), since that correction was about a code that isn't
      on the invoice.
    - Pack size or unit changed: same product, different price per base unit.
      Keep the match (including one a person confirmed) and re-derive the
      price; if it can no longer be derived, send the line back to review.
    - Only numbers changed: re-derive the price from the corrected ones.
    """
    sent = edit.model_fields_set
    before = (line.raw_sku, line.raw_description, line.raw_pack_size, line.uom)

    for name in ("quantity", "unit_price", "extended_price", "raw_description"):
        value = getattr(edit, name)
        if name in sent and value is not None:
            setattr(line, name, value.strip() if isinstance(value, str) else value)
    for name in ("raw_sku", "raw_pack_size"):
        if name in sent and getattr(edit, name) is not None:
            setattr(line, name, getattr(edit, name).strip() or None)
    if "uom" in sent and edit.uom is not None:
        line.uom = edit.uom.strip().upper()

    identity_changed = (line.raw_sku, line.raw_description) != before[:2]
    unit_changed = (line.raw_pack_size, line.uom) != before[2:]

    if identity_changed:
        db.execute(delete(SkuAlias).where(SkuAlias.source_invoice_line_item_id == line.id))
        _match(db, invoice, line)
    elif unit_changed and line.canonical_sku_id is not None:
        result = _exact_match_result(
            db,
            line.canonical_sku_id,
            "repriced",
            line.raw_pack_size,
            line.quantity,
            line.unit_price,
            line.uom,
            line.raw_description,
        )
        line.normalized_qty_base = result.normalized_qty_base
        line.normalized_unit_price = result.normalized_unit_price
        line.base_uom = result.base_uom
        if result.normalized_unit_price is None:
            line.review_status = ReviewStatus.pending
    elif unit_changed:
        _match(db, invoice, line)
    elif {"quantity", "unit_price"} & sent:
        # The line's identity doesn't depend on its price, so its match
        # stands; its price per base unit was computed from the misread
        # number and has to be redone from the corrected one.
        product = db.get(CanonicalSku, line.canonical_sku_id) if line.canonical_sku_id else None
        line.normalized_qty_base, line.normalized_unit_price = normalize_price(
            line.raw_pack_size, line.quantity, line.unit_price, line.uom, product, line.raw_description
        )


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
        raise HTTPException(status_code=404, detail="We couldn't find that invoice.")
    allowed = {InvoiceStatus.needs_review, InvoiceStatus.failed} if editing else {InvoiceStatus.needs_review}
    if invoice.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail="This invoice can't be changed. Only invoices that need a look can be.",
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
    invoice_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: InvoiceEdit,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    lines = {line.id: line for line in _lines(db, invoice.id)}

    # All validation before any mutation, so a bad request changes nothing.
    unknown = [str(edit.id) for edit in body.line_items if edit.id not in lines]
    if unknown:
        raise HTTPException(status_code=422, detail="Some of those items aren't on this invoice any more. Reload the page and try again.")
    sent = body.model_fields_set
    distributor_changed = (
        "distributor_id" in sent and body.distributor_id is not None and body.distributor_id != invoice.distributor_id
    )
    if distributor_changed:
        chosen = db.get(Distributor, body.distributor_id)
        if chosen is None or chosen.slug == UNRECOGNIZED_SLUG or not business_distributors.usable_by(db, tenant_id, chosen):
            raise HTTPException(status_code=422, detail="Choose a distributor from the list.")

    invoice_before = _snapshot(invoice, _INVOICE_AUDITED)
    lines_before = {line.id: _snapshot(line, _LINE_AUDITED) for line in lines.values()}
    status_before = invoice.status

    for name in ("invoice_date", "subtotal", "tax", "total"):
        if name in sent and getattr(body, name) is not None:
            setattr(invoice, name, getattr(body, name))

    for edit in body.line_items:
        _apply_line_edit(db, invoice, lines[edit.id], edit)

    if distributor_changed:
        # Corrections reviewers made on this invoice's lines were recorded as
        # the OLD distributor's item codes. Withdrawn by provenance (the line
        # each was written from), not by (distributor, item code): the same
        # tenant may have made the identical correction on a genuine invoice
        # from that distributor, and that one is still true. Leaving these
        # meant a later real invoice from the old distributor auto-matched
        # the misattributed product through the tenant's own alias.
        db.execute(
            delete(SkuAlias).where(SkuAlias.source_invoice_line_item_id.in_(list(lines)))
        )
        invoice.distributor_id = body.distributor_id
        # Item codes only mean something within one distributor's catalog, so
        # every match made under the old (or no) distributor is void, even a
        # line a person already confirmed: that confirmation was of a code
        # read against the wrong catalog.
        for line in lines.values():
            _match(db, invoice, line)

    _take_under_review(invoice)
    line_changes = {
        str(line.line_number): changed
        for line in lines.values()
        if (changed := audit.changes(lines_before[line.id], _snapshot(line, _LINE_AUDITED)))
    }
    invoice_changes = audit.changes(invoice_before, _snapshot(invoice, _INVOICE_AUDITED))
    if invoice_changes or line_changes or invoice.status != status_before:
        audit.record(
            db,
            user,
            "invoice.edited",
            "invoice",
            invoice.id,
            invoice.tenant_id,
            changes=invoice_changes,
            line_changes=line_changes,
            status={"from": status_before, "to": invoice.status} if invoice.status != status_before else None,
        )
    db.commit()
    return build_invoice_detail(db, invoice)


@router.post("/{invoice_id}/confirm", response_model=InvoiceDetailOut)
def confirm_invoice(
    invoice_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=False)
    lines = _lines(db, invoice.id)

    check = check_stored_invoice(invoice, lines, _distributor(db, invoice), _original(db, invoice))
    if not check.passes:
        # The same rules the worker enforced: confirming is a claim that the
        # numbers are now right, so it can't be granted while they still
        # don't add up.
        raise HTTPException(status_code=422, detail={"message": "Some numbers still don't add up", "reasons": check.reasons})

    invoice.status = InvoiceStatus.confirmed
    tenant = db.get(Tenant, invoice.tenant_id)
    already_observed = set(
        db.scalars(
            select(PriceObservation.invoice_line_item_id).where(
                PriceObservation.invoice_line_item_id.in_([line.id for line in lines])
            )
        )
    )
    observations_written = 0
    for line in lines:
        if line.review_status in _RESOLVED and line.id not in already_observed:
            observation = build_price_observation(line, invoice, tenant)
            if observation is not None:
                db.add(observation)
                observations_written += 1
    wrote_any_observation = observations_written > 0
    audit.record(
        db,
        user,
        "invoice.confirmed",
        "invoice",
        invoice.id,
        invoice.tenant_id,
        total=invoice.total,
        line_count=len(lines),
        price_observations_written=observations_written,
    )
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


def _match_held_lines(db: Session, invoice: Invoice) -> None:
    """Match the lines of an invoice that's no longer held: matching was
    skipped while it was (app/workers/tasks.py)."""
    for line in _lines(db, invoice.id):
        if line.review_status == ReviewStatus.pending and line.canonical_sku_id is None:
            _match(db, invoice, line)


@router.post("/{invoice_id}/keep", response_model=InvoiceDetailOut)
def keep_invoice(
    invoice_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceDetailOut:
    """"It's a different invoice" / "It is an invoice": stop holding it as a
    likely copy or as not an invoice. Its lines, which were kept off Match
    items while it was held, are matched now; the invoice still needs
    confirming like any other held one."""
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    if invoice.duplicate_of_id is None and invoice.document_type is None:
        raise HTTPException(status_code=409, detail="This invoice isn't being held as a copy or as not an invoice.")
    held_as = {"copy_of": invoice.duplicate_of_id, "document_type": invoice.document_type}
    invoice.duplicate_of_id = None
    invoice.document_type = None
    _match_held_lines(db, invoice)
    _take_under_review(invoice)
    audit.record(db, user, "invoice.kept", "invoice", invoice.id, tenant_id, **held_as)
    db.commit()
    return build_invoice_detail(db, invoice)




@router.delete("/{invoice_id}", status_code=204)
def delete_invoice(
    invoice_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> None:
    """Delete an invoice: a copy of one already added, something that isn't
    an invoice, or one added by mistake. Its prices come out of price
    history, alerts and savings with it; the audit trail keeps a record."""
    get_tenant_or_404(db, tenant_id)
    invoice = db.get(Invoice, invoice_id, with_for_update=True)  # tenant-scoped: another location's isn't found
    if invoice is None:
        raise HTTPException(status_code=404, detail="We couldn't find that invoice.")
    # Still being read: the worker holds these and would write lines after.
    if invoice.status in BEING_READ:
        raise HTTPException(status_code=409, detail="It's still being read. Delete it once it's done.")
    line_ids = list(db.scalars(select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id)))
    had_prices = bool(
        line_ids
        and db.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
    )
    distributor = _distributor(db, invoice)
    audit.record(
        db,
        user,
        "invoice.deleted",
        "invoice",
        invoice.id,
        tenant_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        distributor=distributor.name if distributor else None,
        total=invoice.total,
        status=invoice.status,
    )
    # Copies held against this one: once it's gone, each is either a copy of
    # another (the earliest remaining) or the only one, and is matched like
    # any invoice. Otherwise the database would just clear the link, leaving
    # an unheld invoice whose lines were never matched.
    copies = list(
        db.scalars(select(Invoice).where(Invoice.duplicate_of_id == invoice.id).order_by(Invoice.created_at))
    )
    for copy in copies:
        copy.duplicate_of_id = None
    if line_ids:
        db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
        # Matches people made on its lines stay: "this item code is that
        # product" is as true after a copy is deleted. Their link to the
        # line clears itself (ON DELETE SET NULL).
        db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.id.in_(line_ids)))
    db.delete(invoice)
    db.flush()
    for copy in copies:
        original = find_original(db, copy)
        if original is not None:
            copy.duplicate_of_id = original.id
        elif copy.document_type is None:
            _match_held_lines(db, copy)
    db.commit()

    # The stored file and page images; a failure here leaves only orphaned
    # files, never a half-deleted invoice.
    try:
        forget_original(invoice_id)
        get_storage().delete_prefix(renders_prefix(invoice_id))
    except Exception:
        logger.exception("couldn't remove the files of deleted invoice %s", invoice_id)
    if had_prices:
        try:
            upsert_creep_alerts(db, tenant_id)
        except Exception:
            db.rollback()
            logger.exception("creep alert refresh failed after deleting invoice %s", invoice_id)


@router.post("/{invoice_id}/line-items", response_model=InvoiceDetailOut, status_code=201)
def add_line_item(
    invoice_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: LineItemCreate,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
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
    audit.record(
        db,
        user,
        "invoice_line.added",
        "invoice_line_item",
        line.id,
        invoice.tenant_id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        values=_snapshot(line, _LINE_AUDITED),
    )
    db.commit()
    return build_invoice_detail(db, invoice)


@router.delete("/{invoice_id}/line-items/{line_item_id}", response_model=InvoiceDetailOut)
def remove_line_item(
    invoice_id: uuid.UUID,
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceDetailOut:
    """Remove a line that isn't on the page: a mistyped manual entry, or one
    extraction hallucinated. Only on an invoice under review, whose lines by
    definition haven't fed analytics, so there's no observation to unwind."""
    get_tenant_or_404(db, tenant_id)
    invoice = _get_reviewable_invoice(db, invoice_id, editing=True)
    line = db.get(InvoiceLineItem, line_item_id)
    if line is None or line.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="That item isn't on this invoice.")
    # Removing a line says it was never on the invoice, so any correction made
    # on it (line queue confirm/correct) was a correction of nothing. Left in
    # place, a phantom line's alias kept matching that item code for this
    # business, and the ON DELETE SET NULL below would also erase its
    # provenance, so no later re-attribution could find it.
    db.execute(delete(SkuAlias).where(SkuAlias.source_invoice_line_item_id == line.id))
    # The line's content goes into the event: after the delete, this is the
    # only record of what it said.
    audit.record(
        db,
        user,
        "invoice_line.removed",
        "invoice_line_item",
        line.id,
        invoice.tenant_id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        values=_snapshot(line, _LINE_AUDITED),
    )
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
        raise HTTPException(status_code=404, detail="We couldn't find that invoice.")

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
