"""What waits on Match items. One definition, shared by the page, the setup
checklist and the weekly email, so their counts can't disagree.

Needs Invoice and Distributor joined to the line.
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Distributor, Invoice, InvoiceLineItem
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import ReviewStatus


def waiting_to_match():
    return (
        InvoiceLineItem.review_status == ReviewStatus.pending,
        # No catalog to match against until the distributor is known.
        Distributor.slug != UNRECOGNIZED_SLUG,
        # Held as a likely copy, or as not an invoice: nothing on it is used
        # until a person decides, so there's nothing to match yet either.
        Invoice.duplicate_of_id.is_(None),
        Invoice.document_type.is_(None),
    )


def waiting_item_count(db: Session, tenant_id) -> int:
    """How many cards Match items shows: distinct items (per distributor, by
    item code, or description when there's none), not lines. A new location
    had 1,265 waiting lines for about 150 items, each matched once."""
    code = func.nullif(func.upper(func.trim(InvoiceLineItem.raw_sku)), "")
    description = func.upper(func.regexp_replace(func.trim(InvoiceLineItem.raw_description), r"\s+", " ", "g"))
    item = func.concat(Invoice.distributor_id, "|", func.coalesce("sku:" + code, "desc:" + description))
    return db.scalar(
        select(func.count(func.distinct(item)))
        .select_from(InvoiceLineItem)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .join(Distributor, Distributor.id == Invoice.distributor_id)
        .where(InvoiceLineItem.tenant_id == tenant_id, *waiting_to_match())
    )
