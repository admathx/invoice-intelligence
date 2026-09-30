"""SPEC.md §9: the review queue — "the one screen worth making genuinely
fast." Every confirm/correct writes a sku_aliases row (app/normalize/
matcher.py: "This table IS the moat") and, when a price is resolvable, a
price_observations row — this is the real "written after a line item is
confirmed" path price_observation.py's own docstring describes (Phase 4's
seed_corpus_pipeline.py used review_status==auto as a stand-in for this
because this endpoint didn't exist yet).
"""
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import audit
from app.analytics.benchmark import account_key_for
from app.analytics.price_creep import upsert_creep_alerts
from app.api.deps import get_tenant_or_404
from app.auth import current_user, get_db_for_tenant
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
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import BaseUom, ReviewStatus
from app.models.tenant import account_key_column
from app.normalize.matcher import _exact_match_result, apply_match, match_line_item
from app.normalize.pack_size import PackSizeParseError, pack_for_line
from app.packs import apply_to_item, item_key, needs_pack, remember, reprice, same_item
from app.duplicates import BEING_READ
from app.matching_queue import waiting_to_match
from app.schemas.review import (
    AcceptSuggestions,
    AcceptSuggestionsResponse,
    CorrectRequest,
    PackSizeRequest,
    PackSizeResponse,
    ReviewActionResponse,
    ReviewQueueItem,
)

router = APIRouter(prefix="/review", tags=["review"])


def _lock_line_for_action(db: Session, line_item_id: uuid.UUID, *, pending: bool) -> tuple[InvoiceLineItem, Invoice]:
    """The line and its invoice, with the invoice row locked, re-read under the lock.

    The same row the invoice review endpoints and the worker lock. Without it,
    a confirm could interleave with a distributor change on the invoice
    screen: read the old distributor, then write its alias under that
    distributor right after the change had withdrawn exactly those aliases.
    The line is re-read after the lock because its state is what the lock
    protects: a re-attribution may have just re-matched it.
    """
    line = db.get(InvoiceLineItem, line_item_id)
    if line is None:
        raise HTTPException(status_code=404, detail="We couldn't find that item.")
    invoice = db.get(Invoice, line.invoice_id, with_for_update=True, populate_existing=True)
    db.refresh(line)
    if pending and line.review_status != ReviewStatus.pending:
        # Already resolved — a second tab, or a client retrying a request
        # whose commit already landed. Reject rather than write a second
        # sku_aliases/price_observations row for the same line.
        raise HTTPException(status_code=409, detail="That item has already been matched.")
    if not pending and line.review_status == ReviewStatus.pending:
        raise HTTPException(status_code=409, detail="That item is already waiting to be matched.")
    return line, invoice


def _require_recognized_distributor(db: Session, invoice: Invoice) -> None:
    """Item codes only mean something within one distributor's catalog. On an
    invoice nobody has attributed yet, resolving a line here wrote no alias
    and was then overwritten when the distributor was chosen on the invoice
    screen (which re-matches every line), so the effort simply vanished."""
    distributor = db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None
    if distributor is None or distributor.slug == UNRECOGNIZED_SLUG:
        raise HTTPException(
            status_code=409,
            detail="We don't know this invoice's distributor yet. Choose it on the invoice first.",
        )


def _write_alias(
    db: Session, line: InvoiceLineItem, invoice: Invoice, canonical_sku_id: uuid.UUID, user: User
) -> bool:
    if invoice.distributor_id is None:
        return False
    # Extraction's 'other' is a real row but not a catalog. An alias under it
    # would pool item codes from every distributor that ever reached 'other',
    # so one tenant's Sysco correction would match a US Foods code next time.
    distributor = db.get(Distributor, invoice.distributor_id)
    if distributor is None or distributor.slug == UNRECOGNIZED_SLUG:
        return False
    db.add(
        SkuAlias(
            id=uuid.uuid4(),
            # Who corrected it, so matcher.match_by_alias can count how many
            # independent businesses agree before trusting this everywhere.
            tenant_id=line.tenant_id,
            # And on which line, so it can be withdrawn if that invoice turns
            # out to belong to another distributor (invoice_review).
            source_invoice_line_item_id=line.id,
            canonical_sku_id=canonical_sku_id,
            distributor_id=invoice.distributor_id,
            raw_description=line.raw_description,
            raw_sku=line.raw_sku,
            pack_size=line.raw_pack_size,
            confirmed_by_user_id=user.id,
        )
    )
    return True


def _write_observation(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> bool:
    observation = build_price_observation(line, invoice, tenant)
    if observation is None:
        return False
    db.add(observation)
    return True


def _finalize(
    db: Session,
    line: InvoiceLineItem,
    invoice: Invoice,
    tenant: Tenant,
    user: User,
    action: str,
    previous_sku_id: uuid.UUID | None,
    suggested_confidence: Decimal | None,
    refresh_alerts: bool = True,
) -> ReviewActionResponse:
    """Shared by confirm and correct: both write an alias and (when
    resolvable) an observation, settle the item's other waiting lines, commit,
    and refresh the same response shape — the only thing that differs between
    the two endpoints is how `line` got mutated before this runs.
    """
    wrote_alias = _write_alias(db, line, invoice, line.canonical_sku_id, user)
    wrote_observation = _write_observation(db, line, invoice, tenant)
    also_settled, settled_priced = _settle_repeats(db, line, invoice, tenant)
    wrote_observation = wrote_observation or settled_priced
    audit.record(
        db,
        user,
        action,
        "invoice_line_item",
        line.id,
        tenant.id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        raw_sku=line.raw_sku,
        raw_description=line.raw_description,
        canonical_sku={"from": previous_sku_id, "to": line.canonical_sku_id},
        # How sure the matcher was about what the person just judged. With
        # the verdict, this is what calibrates the auto-accept and review
        # thresholds on real invoices (validation/calibration_report.py).
        # "none", not omitted: audit.record drops None, and an absent key is
        # also what verdicts from before this was recorded look like, which
        # the calibration report has to leave out rather than guess at.
        suggested_confidence=suggested_confidence if suggested_confidence is not None else "none",
        review_status=line.review_status,
        also_settled=also_settled or None,
    )
    db.commit()
    db.refresh(line)

    if wrote_observation and refresh_alerts:
        # Refresh this tenant's creep alerts right after new price data
        # actually lands, not on every GET /insights — the previous design
        # made a nominally-safe read into a required write on every page
        # view/refresh/prefetch, recomputing the tenant's entire price
        # history every time regardless of whether anything had changed.
        upsert_creep_alerts(db, tenant.id)

    return ReviewActionResponse(
        id=line.id,
        review_status=line.review_status.value,
        canonical_sku_id=line.canonical_sku_id,
        normalized_unit_price=line.normalized_unit_price,
        wrote_alias=wrote_alias,
        wrote_price_observation=wrote_observation,
        also_settled=also_settled,
    )


def _settle_repeats(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> tuple[int, bool]:
    """The same item waiting on this location's other invoices, settled by
    the decision just made on `line`. A match used to help only later
    invoices, so a new location matched the same item once per invoice it
    was on: on the test set, 1,265 waiting lines for about 150 items.

    Each repeat is priced from its own pack and numbers. One the app can
    price is settled like a remembered match (auto); one it can't takes the
    same status as `line`. A charge marks its repeats as charges.
    Returns (lines settled, whether any price entry was written)."""
    if invoice.distributor_id is None:
        return 0, False
    rows = db.execute(
        select(InvoiceLineItem, Invoice)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .join(Distributor, Distributor.id == Invoice.distributor_id)
        .where(
            InvoiceLineItem.tenant_id == tenant.id,
            InvoiceLineItem.id != line.id,
            Invoice.distributor_id == invoice.distributor_id,
            same_item(item_key(line.raw_sku, line.raw_description)),
            *waiting_to_match(),
        )
        .with_for_update(of=InvoiceLineItem)
    ).all()
    priced = False
    for repeat, repeat_invoice in rows:
        if line.review_status == ReviewStatus.not_product:
            repeat.review_status = ReviewStatus.not_product
            repeat.canonical_sku_id = repeat.match_confidence = repeat.base_uom = None
            repeat.normalized_qty_base = repeat.normalized_unit_price = None
            continue
        result = _exact_match_result(
            db,
            line.canonical_sku_id,
            "repeat",
            repeat.raw_pack_size,
            repeat.quantity,
            repeat.unit_price,
            repeat.uom,
            repeat.raw_description,
        )
        repeat.canonical_sku_id = line.canonical_sku_id
        repeat.match_confidence = Decimal("1.0")
        repeat.normalized_qty_base = result.normalized_qty_base
        repeat.normalized_unit_price = result.normalized_unit_price
        repeat.base_uom = result.base_uom
        repeat.review_status = ReviewStatus.auto if result.normalized_unit_price is not None else line.review_status
        if repeat.review_status != ReviewStatus.pending:
            priced = _write_observation(db, repeat, repeat_invoice, tenant) or priced
    return len(rows), priced


@router.get("/queue", response_model=list[ReviewQueueItem])
def get_review_queue(
    tenant_id: uuid.UUID,
    distributor_id: uuid.UUID | None = None,
    db: Session = Depends(get_db_for_tenant),
) -> list[ReviewQueueItem]:
    # Only lines whose invoice has a recognized distributor: until it does,
    # there's no catalog to resolve an item code against, and whatever a
    # reviewer did here would be overwritten when the distributor is chosen
    # on the invoice screen. Those lines appear once it has been.
    conditions = list(waiting_to_match())
    if distributor_id is not None:
        conditions.append(Invoice.distributor_id == distributor_id)

    rows = db.execute(
        select(InvoiceLineItem, Invoice, Distributor, CanonicalSku)
        .join(Invoice, InvoiceLineItem.invoice_id == Invoice.id)
        .join(Distributor, Invoice.distributor_id == Distributor.id)
        .outerjoin(CanonicalSku, InvoiceLineItem.canonical_sku_id == CanonicalSku.id)
        .where(*conditions)
        .order_by(Invoice.invoice_date, InvoiceLineItem.line_number)
    ).all()

    # One card per item: the same item waiting on several invoices is matched
    # once (_settle_repeats). Shown as its latest line, in order of the
    # earliest invoice it's waiting on.
    groups: dict[tuple, list] = {}
    for row in rows:
        line, invoice = row[0], row[1]
        groups.setdefault((invoice.distributor_id, item_key(line.raw_sku, line.raw_description)), []).append(row)

    return [
        ReviewQueueItem(
            id=line.id,
            invoice_id=invoice.id,
            invoice_date=invoice.invoice_date,
            distributor_name=distributor.name if distributor else None,
            raw_description=line.raw_description,
            raw_sku=line.raw_sku,
            raw_pack_size=line.raw_pack_size,
            quantity=line.quantity,
            unit_price=line.unit_price,
            uom=line.uom,
            canonical_sku_id=line.canonical_sku_id,
            canonical_sku_name=sku.name if sku else None,
            match_confidence=line.match_confidence,
            price_known=line.normalized_unit_price is not None,
            count=len(group),
        )
        for group in groups.values()
        for line, invoice, distributor, sku in [group[-1]]
    ]


@router.post("/{line_item_id}/confirm", response_model=ReviewActionResponse)
def confirm_line_item(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> ReviewActionResponse:
    """Confirms the already-suggested canonical_sku_id is correct — the
    review-band case (0.80-0.92 confidence) where the matcher's guess was
    right. A confirm still writes an alias: the whole point of a human
    verifying a match is that the next identical line resolves for free.
    """
    tenant = get_tenant_or_404(db, tenant_id)
    line, invoice = _lock_line_for_action(db, line_item_id, pending=True)
    _require_recognized_distributor(db, invoice)
    if line.canonical_sku_id is None:
        raise HTTPException(status_code=400, detail="There's no suggestion to confirm. Search for the product instead.")

    line.review_status = ReviewStatus.confirmed
    return _finalize(
        db, line, invoice, tenant, user, "invoice_line.match_confirmed", line.canonical_sku_id, line.match_confidence
    )


_PRICED_PER = {
    BaseUom.lb: "pound",
    BaseUom.oz: "ounce",
    BaseUom.fl_oz: "fluid ounce",
    BaseUom.gal: "gallon",
    BaseUom.each: "item",
    BaseUom.dozen: "dozen",
}
_PACKED_IN = {"lb": "pounds", "oz": "ounces", "gal": "gallons", "dz": "dozens", "ea": "pieces"}


def _require_comparable_units(line: InvoiceLineItem, sku: CanonicalSku) -> None:
    """A product priced per gallon can't take a pack in pounds: the price per
    gallon would be a price per pound under the wrong label, and it would sit
    in that product's history and benchmarks beside real ones. Refused with
    the reason, rather than kept with no price and sent back here every week.
    An unreadable pack isn't this check's concern; that line gets no price
    whatever product it is, and fixing the pack on the invoice prices it."""
    try:
        pack = pack_for_line(line.raw_pack_size, line.uom, line.raw_description)
    except PackSizeParseError:
        return
    if pack.from_description:
        # Only a guess from the item's name: it prices the line if it fits
        # the product, and mustn't stand in the way of the person's choice.
        return
    if pack.base_units_per_pack_unit(sku.base_uom, sku.lb_per_gal) is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{sku.name} is priced per {_PRICED_PER[sku.base_uom]}, and this item comes in "
                f"{_PACKED_IN[pack.unit]} ({line.raw_pack_size}), so their prices can't be compared. "
                "Choose another product, or skip it."
            ),
        )


@router.post("/{line_item_id}/correct", response_model=ReviewActionResponse)
def correct_line_item(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: CorrectRequest,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> ReviewActionResponse:
    """Reassigns canonical_sku_id (whether the line previously had a wrong
    suggestion or none at all) and recomputes normalized price/qty via the
    same pack-size logic the live matcher uses (app/normalize/matcher.py's
    _exact_match_result) — not a second reimplementation of that math.
    """
    tenant = get_tenant_or_404(db, tenant_id)
    line, invoice = _lock_line_for_action(db, line_item_id, pending=True)
    _require_recognized_distributor(db, invoice)
    sku = db.get(CanonicalSku, body.canonical_sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="We couldn't find that product.")
    _require_comparable_units(line, sku)
    previous_sku_id, suggested_confidence = line.canonical_sku_id, line.match_confidence

    result = _exact_match_result(
        db,
        canonical_sku_id=body.canonical_sku_id,
        method="corrected",
        raw_pack_size=line.raw_pack_size,
        quantity=line.quantity,
        unit_price=line.unit_price,
        uom=line.uom,
        raw_description=line.raw_description,
    )
    line.canonical_sku_id = result.canonical_sku_id
    line.normalized_qty_base = result.normalized_qty_base
    line.normalized_unit_price = result.normalized_unit_price
    line.base_uom = result.base_uom
    line.match_confidence = Decimal("1.0")
    # Respect what the matcher actually concluded rather than always claiming
    # `corrected`: if the pack size couldn't be parsed there's no normalized
    # price, and _exact_match_result deliberately returns `pending` for that
    # case ("auto would claim 'no review needed' over a line with no usable
    # price"). Marking it corrected anyway would drop it out of the queue
    # permanently with a null price and no way to ever resurface it.
    line.review_status = (
        ReviewStatus.corrected if result.normalized_unit_price is not None else result.review_status
    )

    return _finalize(
        db, line, invoice, tenant, user, "invoice_line.match_corrected", previous_sku_id, suggested_confidence
    )


@router.post("/accept-suggestions", response_model=AcceptSuggestionsResponse)
def accept_suggestions(
    tenant_id: uuid.UUID,
    body: AcceptSuggestions,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> AcceptSuggestionsResponse:
    """"That's right" on every waiting item whose suggestion is at least this
    sure, and their repeats: a new location's first pass through Match items
    in one step, leaving the doubtful ones for a person. Each is recorded as
    that person's confirmation, as if pressed one by one."""
    tenant = get_tenant_or_404(db, tenant_id)
    items = lines = 0
    wrote_any = False
    for item in get_review_queue(tenant_id, None, db):
        if item.canonical_sku_id is None or item.match_confidence is None or item.match_confidence < body.min_confidence:
            continue
        try:
            line, invoice = _lock_line_for_action(db, item.id, pending=True)
        except HTTPException:
            db.rollback()  # settled meanwhile (a repeat of one just accepted, or another person)
            continue
        line.review_status = ReviewStatus.confirmed
        result = _finalize(
            db,
            line,
            invoice,
            tenant,
            user,
            "invoice_line.match_confirmed",
            line.canonical_sku_id,
            line.match_confidence,
            refresh_alerts=False,
        )
        items += 1
        lines += 1 + result.also_settled
        wrote_any = wrote_any or result.wrote_price_observation
    if wrote_any:
        upsert_creep_alerts(db, tenant.id)
    return AcceptSuggestionsResponse(items=items, lines=lines)


@router.post("/{line_item_id}/pack", response_model=PackSizeResponse)
def set_pack_size(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: PackSizeRequest,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> PackSizeResponse:
    """Enter the pack size for an item whose invoice prints none (or one that
    can't be read), from Match items or the invoice page, on any invoice that
    isn't held: it only changes the price per unit, never the invoice's own
    numbers. It prices this line, is remembered for the item, and fills in
    the item's other lines here that have none (app/packs.py). Correcting a
    readable printed pack fixes this line only."""
    tenant = get_tenant_or_404(db, tenant_id)
    line = db.get(InvoiceLineItem, line_item_id)
    if line is None:
        raise HTTPException(status_code=404, detail="We couldn't find that item.")
    invoice = db.get(Invoice, line.invoice_id, with_for_update=True, populate_existing=True)
    db.refresh(line)
    if invoice.status in BEING_READ:
        raise HTTPException(status_code=409, detail="It's still being read. Try again in a minute.")
    if invoice.duplicate_of_id is not None or invoice.document_type is not None:
        raise HTTPException(status_code=409, detail="This invoice is on hold. Deal with that on the invoice first.")
    if line.review_status == ReviewStatus.not_product:
        raise HTTPException(status_code=409, detail="That's marked as a fee or charge, which has no pack size.")

    pack_size = " ".join(body.pack_size.split()).upper()
    uom = (body.uom or line.uom).strip().upper()
    try:
        parsed = pack_for_line(pack_size, uom)
    except PackSizeParseError:
        parsed = None
    if parsed is None or parsed.from_description:
        raise HTTPException(
            status_code=422,
            detail="We can't read that pack size. Write it the way invoices do, like 4/5 LB, 6/1 GAL, 50 LB or 1000 CT.",
        )

    had_usable_pack = not (line.pack_size_remembered or needs_pack(line.raw_pack_size, line.uom, line.raw_description))
    before = {"pack_size": line.raw_pack_size, "uom": line.uom}
    line.raw_pack_size, line.uom, line.pack_size_remembered = pack_size, uom, False
    priced = reprice(db, line, invoice, tenant)

    applied, remembered_it = 0, False
    distributor = db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None
    if not had_usable_pack and distributor is not None and distributor.slug != UNRECOGNIZED_SLUG:
        key = item_key(line.raw_sku, line.raw_description)
        remember(db, tenant.id, distributor.id, key, pack_size, user.id)
        applied, priced_others = apply_to_item(db, tenant, distributor.id, key, pack_size, except_line_id=line.id)
        priced = priced or priced_others
        remembered_it = True
    audit.record(
        db,
        user,
        "invoice_line.pack_set",
        "invoice_line_item",
        line.id,
        tenant.id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        raw_description=line.raw_description,
        changes=audit.changes(before, {"pack_size": line.raw_pack_size, "uom": line.uom}),
        remembered=remembered_it or None,
        applied_to=applied or None,
    )
    db.commit()
    db.refresh(line)
    if priced:
        try:
            upsert_creep_alerts(db, tenant.id)
        except Exception:
            db.rollback()
    return PackSizeResponse(
        id=line.id,
        raw_pack_size=line.raw_pack_size,
        uom=line.uom,
        review_status=line.review_status.value,
        normalized_unit_price=line.normalized_unit_price,
        price_known=line.normalized_unit_price is not None,
        remembered=remembered_it,
        applied_to=applied,
    )


@router.post("/{line_item_id}/not-product", response_model=ReviewActionResponse)
def mark_not_a_product(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> ReviewActionResponse:
    """A fee, deposit or discount the wording check didn't recognize
    (app/extract/charges.py): nothing to match, so it leaves Match items.
    No alias is written; "Wrong product?" (reopen) brings it back."""
    tenant = get_tenant_or_404(db, tenant_id)
    line, invoice = _lock_line_for_action(db, line_item_id, pending=True)
    previous_sku_id = line.canonical_sku_id
    line.review_status = ReviewStatus.not_product
    line.canonical_sku_id = line.match_confidence = line.base_uom = None
    line.normalized_qty_base = line.normalized_unit_price = None
    also_settled, _ = _settle_repeats(db, line, invoice, tenant)
    audit.record(
        db,
        user,
        "invoice_line.not_a_product",
        "invoice_line_item",
        line.id,
        tenant.id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        raw_description=line.raw_description,
        suggested=previous_sku_id,
        also_settled=also_settled or None,
    )
    db.commit()
    return ReviewActionResponse(
        id=line.id,
        review_status=line.review_status.value,
        canonical_sku_id=None,
        normalized_unit_price=None,
        wrote_alias=False,
        wrote_price_observation=False,
        also_settled=also_settled,
    )


@router.post("/{line_item_id}/reopen", response_model=ReviewActionResponse)
def reopen_line_item(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> ReviewActionResponse:
    """Sends an already-resolved line back to the review queue.

    Without this there is no path at all to fix a bad auto-match: the
    matcher accepts anything scoring >= AUTO_MATCH_CONFIDENCE_THRESHOLD
    without a human ever seeing it, and every other endpoint here rejects a
    line whose review_status isn't `pending`. An embedding false positive at
    or above threshold would otherwise sit in price_observations forever,
    quietly skewing benchmarks for every tenant in the cell.

    Any price observation this line produced is deleted on the way out — the
    number is disputed, so it should stop feeding analytics immediately
    rather than linger until someone re-resolves the line. So is the alias the
    line's resolution wrote (see below).
    """
    tenant = get_tenant_or_404(db, tenant_id)
    line, invoice = _lock_line_for_action(db, line_item_id, pending=False)

    db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id == line.id))

    # And the alias this line's confirm/correct wrote, for the same reason:
    # the mapping is what's disputed. Leaving it meant the tenant's own alias
    # (match_by_alias rule 1) auto-matched the same code to the same wrong SKU
    # at confidence 1.0 on the very next invoice, and it kept counting as this
    # business's vote for that mapping toward cross-tenant promotion.
    #
    # Scoped to this BUSINESS, not this tenant, because that is the scope
    # rule 1 trusts: a sibling location's copy of the same wrong mapping would
    # otherwise keep auto-matching for the very location disputing it. Other
    # businesses' corrections are theirs and stay.
    if invoice.distributor_id is not None and line.canonical_sku_id is not None:
        business = account_key_for(db, line.tenant_id)
        same_business = select(Tenant.id).where(account_key_column() == business)
        db.execute(
            delete(SkuAlias).where(
                SkuAlias.tenant_id.in_(same_business),
                SkuAlias.distributor_id == invoice.distributor_id,
                SkuAlias.raw_sku == line.raw_sku,
                SkuAlias.canonical_sku_id == line.canonical_sku_id,
            )
        )

    audit.record(
        db,
        user,
        "invoice_line.reopened",
        "invoice_line_item",
        line.id,
        tenant.id,
        invoice_id=invoice.id,
        line_number=line.line_number,
        raw_sku=line.raw_sku,
        raw_description=line.raw_description,
        canonical_sku_id=line.canonical_sku_id,
        # A reopened `auto` line is a match the matcher accepted on its own
        # and a person disputed: a false positive at this confidence.
        match_confidence=line.match_confidence,
        review_status={"from": line.review_status, "to": ReviewStatus.pending},
    )
    if line.review_status == ReviewStatus.not_product:
        # "It's a product after all": find it a suggestion, as for any new
        # line, so Match items doesn't start from a blank search.
        distributor = db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None
        if distributor is not None and distributor.slug != UNRECOGNIZED_SLUG:
            apply_match(
                line,
                match_line_item(
                    db,
                    distributor_id=distributor.id,
                    raw_sku=line.raw_sku,
                    raw_description=line.raw_description,
                    raw_pack_size=line.raw_pack_size,
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    uom=line.uom,
                    tenant_id=line.tenant_id,
                ),
            )
    line.review_status = ReviewStatus.pending
    db.commit()
    db.refresh(line)

    # The disputed observation is gone, so this tenant's creep alerts are now
    # computed from stale inputs until they're recomputed.
    upsert_creep_alerts(db, tenant.id)

    return ReviewActionResponse(
        id=line.id,
        review_status=line.review_status.value,
        canonical_sku_id=line.canonical_sku_id,
        normalized_unit_price=line.normalized_unit_price,
        wrote_alias=False,
        wrote_price_observation=False,
    )
