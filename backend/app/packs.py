"""Pack sizes a person enters, remembered for the item.

Plenty of invoices print no pack size, or one that can't be read, and a
price per pound or gallon can't be worked out without it. On the test set
that left about one item in ten unpriced, every week, with nothing a person
could do: the note on Match items said to fix it on the invoice, which a
"Ready" invoice doesn't allow. Now a person enters it once (Match items, or
the invoice page) and it is:

- used on that line and on the item's earlier lines at the location that
  have none, so their prices count;
- remembered per business, distributor and item, and filled in on every
  later invoice (the worker), marked as remembered.

A printed pack that can be read is never overridden: a person correcting a
misread one fixes that line only.
"""
import uuid
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.analytics.benchmark import account_key_for
from app.matching_queue import is_repeat, item_key
from app.models import Invoice, InvoiceLineItem, PriceObservation, RememberedPack, Tenant, build_price_observation
from app.models.enums import ReviewStatus
from app.normalize.description_expansion import description_similarity
from app.normalize.matcher import MIN_ALIAS_DESCRIPTION_SIMILARITY, _exact_match_result, apply_match, match_line_item
from app.normalize.pack_size import PackSizeParseError, pack_for_line

RESOLVED = (ReviewStatus.auto, ReviewStatus.confirmed, ReviewStatus.corrected)
EXACT = Decimal("1.0")


def needs_pack(raw_pack_size: str | None, uom: str, raw_description: str | None) -> bool:
    """No pack the app can use from the page: none printed, one it can't
    read, or only a guess from the description."""
    try:
        return pack_for_line(raw_pack_size, uom, raw_description).from_description
    except PackSizeParseError:
        return True


def set_pack(line: InvoiceLineItem, pack_size: str, *, remembered: bool) -> None:
    """Put an entered or remembered pack on a line, keeping what the invoice
    printed if this replaces it."""
    if line.raw_pack_size and line.printed_pack_size is None and not line.pack_size_remembered:
        line.printed_pack_size = line.raw_pack_size
    line.raw_pack_size = pack_size
    line.pack_size_remembered = remembered


def remembered(db: Session, tenant_id: uuid.UUID, distributor_id: uuid.UUID) -> dict[str, tuple[str, str | None]]:
    """This business's remembered packs for a distributor: item -> (pack,
    description it was entered for)."""
    return {
        key: (pack, description)
        for key, pack, description in db.execute(
            select(RememberedPack.item_key, RememberedPack.pack_size, RememberedPack.raw_description).where(
                RememberedPack.account_key == account_key_for(db, tenant_id),
                RememberedPack.distributor_id == distributor_id,
            )
        )
    }


def remembered_pack_for(
    packs: dict[str, tuple[str, str | None]], raw_sku: str | None, raw_description: str | None
) -> str | None:
    """The remembered pack for a line, if its item has one and its
    description still looks like the item's (a reused code doesn't inherit
    another product's pack)."""
    found = packs.get(item_key(raw_sku, raw_description))
    if found is None:
        return None
    pack, description = found
    if description and description_similarity(description, raw_description or "") < MIN_ALIAS_DESCRIPTION_SIMILARITY:
        return None
    return pack


def remember(
    db: Session, tenant_id: uuid.UUID, distributor_id: uuid.UUID, line: InvoiceLineItem, user_id: uuid.UUID | None
) -> None:
    """Remember `line`'s pack for its item."""
    stmt = pg_insert(RememberedPack).values(
        id=uuid.uuid4(),
        account_key=account_key_for(db, tenant_id),
        distributor_id=distributor_id,
        item_key=item_key(line.raw_sku, line.raw_description),
        pack_size=line.raw_pack_size,
        raw_description=line.raw_description,
        set_by_user_id=user_id,
    )
    db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_remembered_packs_item",
            set_={
                "pack_size": stmt.excluded.pack_size,
                "raw_description": stmt.excluded.raw_description,
                "set_by_user_id": user_id,
                "updated_at": func.now(),
            },
        )
    )


def lock_invoices(db: Session, invoice_ids) -> set:
    """Lock these invoices, as every other writer of their lines does (one
    at a time), so a re-attribution re-matching the same lines can't
    interleave. Returns the ones locked: any another request holds right now
    is skipped rather than waited for (the caller already holds one invoice,
    and waiting on more could deadlock); its lines are simply left as they
    are, to be settled another time."""
    ids = set(invoice_ids)
    if not ids:
        return set()
    return set(
        db.scalars(select(Invoice.id).where(Invoice.id.in_(ids)).order_by(Invoice.id).with_for_update(skip_locked=True))
    )


def reprice(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> bool:
    """Work a line's price out again after its pack or unit changed, and its
    price history entry with it. A line with a product keeps it: a person may
    have chosen it (a correction stays waiting when it has no price), and a
    suggestion is still the suggestion. Once priced, one whose identity is
    certain (a remembered or corrected match) is settled. A line with no
    product yet is matched afresh with the new pack. True if it now has a
    price entry."""
    db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id == line.id))
    if line.canonical_sku_id is not None:
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
        if line.review_status == ReviewStatus.pending and line.match_confidence == EXACT and result.normalized_unit_price is not None:
            line.review_status = ReviewStatus.auto
    elif line.review_status == ReviewStatus.pending and invoice.distributor_id is not None:
        apply_match(
            line,
            match_line_item(
                db,
                distributor_id=invoice.distributor_id,
                raw_sku=line.raw_sku,
                raw_description=line.raw_description,
                raw_pack_size=line.raw_pack_size,
                quantity=line.quantity,
                unit_price=line.unit_price,
                uom=line.uom,
                tenant_id=line.tenant_id,
            ),
        )
    if line.review_status not in RESOLVED:
        return False
    observation = build_price_observation(line, invoice, tenant)
    if observation is None:
        return False
    db.add(observation)
    return True


def apply_to_item(db: Session, tenant: Tenant, source: InvoiceLineItem, distributor_id: uuid.UUID) -> tuple[int, bool]:
    """Fill `source`'s pack into this location's other lines of the same item
    (is_repeat) that have none the app can use (or an older remembered one),
    and price them. Not lines on invoices held as copies or as not invoices.
    Returns (lines updated, whether any price entry was written)."""
    rows = [
        (line, invoice)
        for line, invoice in db.execute(
            select(InvoiceLineItem, Invoice)
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .where(
                InvoiceLineItem.tenant_id == tenant.id,
                InvoiceLineItem.id != source.id,
                InvoiceLineItem.review_status != ReviewStatus.not_product,
                Invoice.distributor_id == distributor_id,
                Invoice.duplicate_of_id.is_(None),
                Invoice.document_type.is_(None),
            )
        ).all()
        if is_repeat(source, line)
        and (line.pack_size_remembered or needs_pack(line.raw_pack_size, line.uom, line.raw_description))
    ]
    locked = lock_invoices(db, [invoice.id for _, invoice in rows])
    updated, priced = 0, False
    for line, invoice in rows:
        if invoice.id not in locked:
            continue
        db.refresh(line)  # as it is under the lock
        if not (line.pack_size_remembered or needs_pack(line.raw_pack_size, line.uom, line.raw_description)):
            continue
        set_pack(line, source.raw_pack_size, remembered=True)
        priced = reprice(db, line, invoice, tenant) or priced
        updated += 1
    return updated, priced
