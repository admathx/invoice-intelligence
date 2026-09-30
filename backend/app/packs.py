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

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.analytics.benchmark import account_key_for
from app.models import Invoice, InvoiceLineItem, PriceObservation, RememberedPack, Tenant, build_price_observation
from app.models.enums import ReviewStatus
from app.normalize.matcher import _exact_match_result, apply_match, match_line_item
from app.normalize.pack_size import PackSizeParseError, pack_for_line

RESOLVED = (ReviewStatus.auto, ReviewStatus.confirmed, ReviewStatus.corrected)


def item_key(raw_sku: str | None, raw_description: str | None) -> str:
    """Which item a line is, for remembering things about it: its item code,
    or its description when it has none."""
    sku = (raw_sku or "").strip().upper()
    if sku:
        return f"sku:{sku}"
    return "desc:" + " ".join((raw_description or "").upper().split())


def same_item(key: str):
    """SQL: a line that is this item."""
    kind, value = key.split(":", 1)
    if kind == "sku":
        return func.upper(func.trim(InvoiceLineItem.raw_sku)) == value
    return and_(
        or_(InvoiceLineItem.raw_sku.is_(None), func.trim(InvoiceLineItem.raw_sku) == ""),
        func.upper(func.regexp_replace(func.trim(InvoiceLineItem.raw_description), r"\s+", " ", "g")) == value,
    )


def needs_pack(raw_pack_size: str | None, uom: str, raw_description: str | None) -> bool:
    """No pack the app can use from the page: none printed, one it can't
    read, or only a guess from the description."""
    try:
        return pack_for_line(raw_pack_size, uom, raw_description).from_description
    except PackSizeParseError:
        return True


def remembered(db: Session, tenant_id: uuid.UUID, distributor_id: uuid.UUID) -> dict[str, str]:
    """This business's remembered packs for a distributor, by item."""
    return dict(
        db.execute(
            select(RememberedPack.item_key, RememberedPack.pack_size).where(
                RememberedPack.account_key == account_key_for(db, tenant_id),
                RememberedPack.distributor_id == distributor_id,
            )
        ).all()
    )


def remember(
    db: Session, tenant_id: uuid.UUID, distributor_id: uuid.UUID, key: str, pack_size: str, user_id: uuid.UUID | None
) -> None:
    stmt = pg_insert(RememberedPack).values(
        id=uuid.uuid4(),
        account_key=account_key_for(db, tenant_id),
        distributor_id=distributor_id,
        item_key=key,
        pack_size=pack_size,
        set_by_user_id=user_id,
    )
    db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_remembered_packs_item",
            set_={"pack_size": stmt.excluded.pack_size, "set_by_user_id": user_id, "updated_at": func.now()},
        )
    )


def reprice(db: Session, line: InvoiceLineItem, invoice: Invoice, tenant: Tenant) -> bool:
    """Work a line's price out again after its pack or unit changed, and its
    price history entry with it. A line still waiting is matched afresh (a
    remembered match with a price now settles itself); a settled one keeps
    its product and status. True if it now has a price entry."""
    db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id == line.id))
    if line.review_status == ReviewStatus.pending and invoice.distributor_id is not None:
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
    elif line.canonical_sku_id is not None:
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
    if line.review_status not in RESOLVED:
        return False
    observation = build_price_observation(line, invoice, tenant)
    if observation is None:
        return False
    db.add(observation)
    return True


def apply_to_item(
    db: Session, tenant: Tenant, distributor_id: uuid.UUID, key: str, pack_size: str, except_line_id: uuid.UUID
) -> tuple[int, bool]:
    """Fill a remembered pack into this location's other lines of the item
    that have none the app can use (or an older remembered one), and price
    them. Not lines on invoices held as copies or as not invoices. Returns
    (lines updated, whether any price entry was written)."""
    rows = db.execute(
        select(InvoiceLineItem, Invoice)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(
            InvoiceLineItem.tenant_id == tenant.id,
            InvoiceLineItem.id != except_line_id,
            InvoiceLineItem.review_status != ReviewStatus.not_product,
            Invoice.distributor_id == distributor_id,
            Invoice.duplicate_of_id.is_(None),
            Invoice.document_type.is_(None),
            same_item(key),
        )
    ).all()
    updated, priced = 0, False
    for line, invoice in rows:
        if not (line.pack_size_remembered or needs_pack(line.raw_pack_size, line.uom, line.raw_description)):
            continue
        line.raw_pack_size = pack_size
        line.pack_size_remembered = True
        priced = reprice(db, line, invoice, tenant) or priced
        updated += 1
    return updated, priced
