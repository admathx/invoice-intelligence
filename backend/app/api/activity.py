"""Reading the audit trail: a location's recent activity, one invoice's
history, and the operators' log across everything.

The first two go through get_db_for_tenant, so a person sees the trail of
exactly the locations they can open. audit_events isn't TenantScoped (see
app/models/user.py), so every query here filters on tenant_id itself.
"""
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.auth import get_db_for_tenant, require_operator
from app.db import get_db
from app.models import AuditEvent, Invoice, Tenant, User
from app.schemas.auth import AuditEventOut, AuditPage

router = APIRouter(tags=["activity"])

MAX_EVENTS = 500
# Newest first; id breaks ties so a page boundary can never skip or repeat an
# event that shares its timestamp with another.
_NEWEST_FIRST = (AuditEvent.occurred_at.desc(), AuditEvent.id.desc())


def _events(db: Session, *conditions, limit: int) -> list[AuditEventOut]:
    rows = db.execute(
        select(AuditEvent, User.name, User.email, Tenant.name)
        .outerjoin(User, User.id == AuditEvent.actor_user_id)
        .outerjoin(Tenant, Tenant.id == AuditEvent.tenant_id)
        .where(*conditions)
        .order_by(*_NEWEST_FIRST)
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
            tenant_id=event.tenant_id,
            tenant_name=tenant_name,
            details=event.details,
        )
        for event, name, email, tenant_name in rows
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


# An action name or a family prefix ("user.", "invoice_line."), nothing else.
_ACTION_PREFIX = re.compile(r"[a-z_]+\.?[a-z_]*")


def _cursor(event: AuditEventOut) -> str:
    return f"{event.occurred_at.isoformat()}|{event.id}"


def _parse_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        when, event_id = cursor.split("|")
        return datetime.fromisoformat(when), uuid.UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


@router.get("/audit", response_model=AuditPage, dependencies=[Depends(require_operator)])
def audit_log(
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
) -> AuditPage:
    """Operators only: every event, including those tied to no location
    (businesses, logins, failed sign-ins). `action` is a prefix: "user."
    for all user management, "invoice_line." for the review queue."""
    conditions = []
    if action:
        if not _ACTION_PREFIX.fullmatch(action):
            raise HTTPException(status_code=422, detail="action must be an action name or prefix like 'user.'")
        # autoescape: "_" is a LIKE wildcard, and "invoice_" must not match
        # "invoiceX". "invoice." and "invoice_line." are distinct families.
        conditions.append(AuditEvent.action.startswith(action, autoescape=True))
    if actor_id:
        conditions.append(AuditEvent.actor_user_id == actor_id)
    if tenant_id:
        conditions.append(AuditEvent.tenant_id == tenant_id)
    if entity_id:
        # The thing itself, or a line on it (line events name their invoice).
        conditions.append(
            or_(AuditEvent.entity_id == entity_id, AuditEvent.details["invoice_id"].astext == str(entity_id))
        )
    if cursor:
        conditions.append(tuple_(AuditEvent.occurred_at, AuditEvent.id) < _parse_cursor(cursor))

    limit = max(1, min(limit, MAX_EVENTS))
    # One extra row says whether another page exists without a count query.
    events = _events(db, *conditions, limit=limit + 1)
    more = len(events) > limit
    events = events[:limit]
    return AuditPage(events=events, next_cursor=_cursor(events[-1]) if more else None)
