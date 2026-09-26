"""Reading the audit trail: a location's recent activity, and one invoice's
history.

Both go through get_db_for_tenant, so a person sees the trail of exactly the
locations they can open. audit_events isn't TenantScoped (see
app/models/user.py), so every query here filters on tenant_id itself.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.auth import get_db_for_tenant
from app.models import AuditEvent, Invoice, User
from app.schemas.auth import AuditEventOut

router = APIRouter(tags=["activity"])

MAX_EVENTS = 500


def _events(db: Session, *conditions, limit: int) -> list[AuditEventOut]:
    rows = db.execute(
        select(AuditEvent, User.name, User.email)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .where(*conditions)
        .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id)
        .limit(limit)
    ).all()
    return [
        AuditEventOut(
            id=event.id,
            occurred_at=event.occurred_at,
            actor_name=name,
            actor_email=email,
            action=event.action,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            details=event.details,
        )
        for event, name, email in rows
    ]


@router.get("/activity", response_model=list[AuditEventOut])
def location_activity(
    tenant_id: uuid.UUID, limit: int = 100, db: Session = Depends(get_db_for_tenant)
) -> list[AuditEventOut]:
    get_tenant_or_404(db, tenant_id)
    return _events(db, AuditEvent.tenant_id == tenant_id, limit=max(1, min(limit, MAX_EVENTS)))


@router.get("/invoices/{invoice_id}/history", response_model=list[AuditEventOut])
def invoice_history(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> list[AuditEventOut]:
    """Everything that happened to this invoice and its lines, newest first.

    Line events carry their invoice's id in details (app.audit callers do
    this), because a removed line's own id no longer leads anywhere.
    """
    get_tenant_or_404(db, tenant_id)
    if db.get(Invoice, invoice_id) is None:  # tenant-scoped: another location's id is simply not found
        raise HTTPException(status_code=404, detail="invoice not found")
    return _events(
        db,
        AuditEvent.tenant_id == tenant_id,
        or_(AuditEvent.entity_id == invoice_id, AuditEvent.details["invoice_id"].astext == str(invoice_id)),
        limit=MAX_EVENTS,
    )
