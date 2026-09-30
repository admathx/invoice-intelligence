"""What waits on Match items. One definition, shared by the page, the setup
checklist and the weekly email, so their counts can't disagree.

Needs Invoice and Distributor joined to the line.
"""
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
