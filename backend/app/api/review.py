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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.price_creep import upsert_creep_alerts
from app.db import get_db_for_tenant
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, SkuAlias, Tenant, build_price_observation
from app.models.enums import ReviewStatus
from app.normalize.matcher import _exact_match_result
from app.schemas.review import CorrectRequest, ReviewActionResponse, ReviewQueueItem

router = APIRouter(prefix="/review", tags=["review"])


def _get_tenant_or_404(db: Session, tenant_id: uuid.UUID) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


def _get_pending_line_or_404(db: Session, line_item_id: uuid.UUID) -> InvoiceLineItem:
    line = db.get(InvoiceLineItem, line_item_id)
    if line is None:
        raise HTTPException(status_code=404, detail="line item not found")
    if line.review_status != ReviewStatus.pending:
        # Already resolved — a second tab, or a client retrying a request
        # whose commit already landed. Reject rather than write a second
        # sku_aliases/price_observations row for the same line.
        raise HTTPException(status_code=409, detail=f"line item is already {line.review_status.value}, not pending")
    return line


def _write_alias(db: Session, line: InvoiceLineItem, invoice: Invoice, canonical_sku_id: uuid.UUID) -> bool:
    if invoice.distributor_id is None:
        return False
    db.add(
        SkuAlias(
            id=uuid.uuid4(),
            canonical_sku_id=canonical_sku_id,
            distributor_id=invoice.distributor_id,
            raw_description=line.raw_description,
            raw_sku=line.raw_sku,
            pack_size=line.raw_pack_size,
        )
    )
    return True


def _write_observation(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> bool:
    observation = build_price_observation(line, invoice, tenant)
    if observation is None:
        return False
    db.add(observation)
    return True


def _finalize(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> ReviewActionResponse:
    """Shared by confirm and correct: both write an alias and (when
    resolvable) an observation, commit, and refresh the same response shape
    — the only thing that differs between the two endpoints is how `line`
    got mutated before this runs.
    """
    wrote_alias = _write_alias(db, line, invoice, line.canonical_sku_id)
    wrote_observation = _write_observation(db, line, invoice, tenant)
    db.commit()
    db.refresh(line)

    if wrote_observation:
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
    )


@router.get("/queue", response_model=list[ReviewQueueItem])
def get_review_queue(
    tenant_id: uuid.UUID,
    distributor_id: uuid.UUID | None = None,
    db: Session = Depends(get_db_for_tenant),
) -> list[ReviewQueueItem]:
    conditions = [InvoiceLineItem.review_status == ReviewStatus.pending]
    if distributor_id is not None:
        conditions.append(Invoice.distributor_id == distributor_id)

    rows = db.execute(
        select(InvoiceLineItem, Invoice, Distributor, CanonicalSku)
        .join(Invoice, InvoiceLineItem.invoice_id == Invoice.id)
        .outerjoin(Distributor, Invoice.distributor_id == Distributor.id)
        .outerjoin(CanonicalSku, InvoiceLineItem.canonical_sku_id == CanonicalSku.id)
        .where(*conditions)
        .order_by(Invoice.invoice_date, InvoiceLineItem.line_number)
    ).all()

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
        )
        for line, invoice, distributor, sku in rows
    ]


@router.post("/{line_item_id}/confirm", response_model=ReviewActionResponse)
def confirm_line_item(
    line_item_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> ReviewActionResponse:
    """Confirms the already-suggested canonical_sku_id is correct — the
    review-band case (0.80-0.92 confidence) where the matcher's guess was
    right. A confirm still writes an alias: the whole point of a human
    verifying a match is that the next identical line resolves for free.
    """
    line = _get_pending_line_or_404(db, line_item_id)
    if line.canonical_sku_id is None:
        raise HTTPException(status_code=400, detail="no suggested match to confirm — use /correct instead")

    invoice = db.get(Invoice, line.invoice_id)
    tenant = _get_tenant_or_404(db, tenant_id)

    line.review_status = ReviewStatus.confirmed
    return _finalize(db, line, invoice, tenant)


@router.post("/{line_item_id}/correct", response_model=ReviewActionResponse)
def correct_line_item(
    line_item_id: uuid.UUID,
    tenant_id: uuid.UUID,
    body: CorrectRequest,
    db: Session = Depends(get_db_for_tenant),
) -> ReviewActionResponse:
    """Reassigns canonical_sku_id (whether the line previously had a wrong
    suggestion or none at all) and recomputes normalized price/qty via the
    same pack-size logic the live matcher uses (app/normalize/matcher.py's
    _exact_match_result) — not a second reimplementation of that math.
    """
    line = _get_pending_line_or_404(db, line_item_id)
    sku = db.get(CanonicalSku, body.canonical_sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="canonical SKU not found")

    invoice = db.get(Invoice, line.invoice_id)
    tenant = _get_tenant_or_404(db, tenant_id)

    result = _exact_match_result(
        db,
        canonical_sku_id=body.canonical_sku_id,
        method="corrected",
        raw_pack_size=line.raw_pack_size,
        quantity=line.quantity,
        unit_price=line.unit_price,
        uom=line.uom,
    )
    line.canonical_sku_id = result.canonical_sku_id
    line.normalized_qty_base = result.normalized_qty_base
    line.normalized_unit_price = result.normalized_unit_price
    line.base_uom = result.base_uom
    line.match_confidence = Decimal("1.0")
    line.review_status = ReviewStatus.corrected

    return _finalize(db, line, invoice, tenant)
