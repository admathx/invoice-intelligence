"""What waits on Match items, and what counts as the same item there. One
definition, shared by the page, the setup checklist, the weekly email,
settling an item's repeats (app/api/review.py) and remembering pack sizes
(app/packs.py), so none of them can disagree about it.
"""
from collections.abc import Iterable, Sequence
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Distributor, Invoice, InvoiceLineItem
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import ReviewStatus
from app.normalize.description_expansion import description_similarity
from app.normalize.matcher import MIN_ALIAS_DESCRIPTION_SIMILARITY


def waiting_to_match():
    """SQL conditions; needs Invoice and Distributor joined to the line."""
    return (
        InvoiceLineItem.review_status == ReviewStatus.pending,
        # No catalog to match against until the distributor is known.
        Distributor.slug != UNRECOGNIZED_SLUG,
        # Held as a likely copy, or as not an invoice: nothing on it is used
        # until a person decides, so there's nothing to match yet either.
        Invoice.duplicate_of_id.is_(None),
        Invoice.document_type.is_(None),
    )


def item_key(raw_sku: str | None, raw_description: str | None) -> str:
    """Which item a line is: its item code, or its description when it has
    none. Whitespace and case don't count."""
    sku = "".join((raw_sku or "").split()).upper()
    if sku:
        return f"sku:{sku}"
    return "desc:" + " ".join((raw_description or "").upper().split())


def is_repeat(a: InvoiceLineItem, b: InvoiceLineItem) -> bool:
    """Whether two lines (of one distributor) are the same item: the same
    code, and a description that still looks like the same thing. The second
    half is what a remembered match checks too (match_by_alias): a
    distributor reusing a retired code for another product mustn't have one
    product's decision, or pack, applied to the other."""
    if item_key(a.raw_sku, a.raw_description) != item_key(b.raw_sku, b.raw_description):
        return False
    return description_similarity(a.raw_description, b.raw_description) >= MIN_ALIAS_DESCRIPTION_SIMILARITY


T = TypeVar("T")


def group_repeats(items: Iterable[T], line_of, distributor_of) -> list[list[T]]:
    """Items grouped by the same distributor and item (is_repeat, compared
    with each group's first), in order of first appearance."""
    groups: list[list[T]] = []
    by_key: dict[tuple, list[int]] = {}
    for item in items:
        line = line_of(item)
        key = (distributor_of(item), item_key(line.raw_sku, line.raw_description))
        for i in by_key.get(key, []):
            if is_repeat(line_of(groups[i][0]), line):
                groups[i].append(item)
                break
        else:
            by_key.setdefault(key, []).append(len(groups))
            groups.append([item])
    return groups


def waiting_rows(db: Session, tenant_id) -> Sequence:
    """(line, invoice) waiting on Match items, oldest invoice first."""
    return db.execute(
        select(InvoiceLineItem, Invoice)
        .join(Invoice, InvoiceLineItem.invoice_id == Invoice.id)
        .join(Distributor, Invoice.distributor_id == Distributor.id)
        .where(InvoiceLineItem.tenant_id == tenant_id, *waiting_to_match())
        .order_by(Invoice.invoice_date, InvoiceLineItem.line_number)
    ).all()


def waiting_item_count(db: Session, tenant_id) -> int:
    """How many cards Match items shows: items, not lines. A new location
    had 1,265 waiting lines for about 150 items, each matched once."""
    rows = waiting_rows(db, tenant_id)
    return len(group_repeats(rows, lambda r: r[0], lambda r: r[1].distributor_id))
