"""A new location's first steps, and which are done: the checklist at the
top of the Invoices page until everything's in place.

Each step is worked out from what has actually happened (an invoice
arrived, one came by email, nothing is waiting to be matched, a second
person has access), so it ticks itself off; nobody has to mark it.
"""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.auth import get_db_for_tenant
from app.matching_queue import waiting_item_count
from app.models import Invoice, TenantMembership
from app.models.enums import InvoiceSource

router = APIRouter(prefix="/setup", tags=["setup"])


class SetupOut(BaseModel):
    first_invoice: bool
    emailed_invoice: bool
    items_matched: bool
    team_added: bool
    inbox_address: str | None
    waiting_to_match: int


@router.get("", response_model=SetupOut)
def setup_steps(tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)) -> SetupOut:
    tenant = get_tenant_or_404(db, tenant_id)
    first_invoice = db.scalar(select(exists().where(Invoice.tenant_id == tenant_id)))
    emailed = db.scalar(
        select(exists().where(Invoice.tenant_id == tenant_id, Invoice.source == InvoiceSource.email))
    )
    # Counted as the Match items page lists them (app/api/review.py).
    waiting = waiting_item_count(db, tenant_id)
    members = db.scalar(select(func.count(TenantMembership.id)).where(TenantMembership.tenant_id == tenant_id))
    return SetupOut(
        first_invoice=first_invoice,
        emailed_invoice=emailed,
        # Only once there's been something to match: an empty location
        # hasn't matched anything.
        items_matched=first_invoice and waiting == 0,
        team_added=members >= 2,
        inbox_address=tenant.inbox_address,
        waiting_to_match=waiting,
    )
