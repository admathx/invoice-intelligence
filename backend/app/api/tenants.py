"""Locations (tenants): listing them for the Businesses screen, and creating
one when a new restaurant signs up.

Operators only: this returns every tenant in the system. A member's own
locations come from GET /auth/me.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import get_tenant_or_404
from app.auth import require_operator
from app.db import get_db
from app.ingest.email_stub import inbox_address_for
from app.models import Tenant, User
from app.schemas.accounts import TenantCreate, TenantSummary

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[Depends(require_operator)])


def _summary(t: Tenant) -> TenantSummary:
    return TenantSummary(
        id=t.id,
        name=t.name,
        metro=t.metro,
        volume_tier=t.volume_tier.value,
        account_id=t.account_id,
        inbox_address=t.inbox_address,
    )


@router.get("", response_model=list[TenantSummary])
def list_tenants(unassigned: bool = False, db: Session = Depends(get_db)) -> list[TenantSummary]:
    """`unassigned=true` returns only tenants not yet part of a business —
    the candidates for attaching to an account.
    """
    query = select(Tenant).order_by(Tenant.name)
    if unassigned:
        query = query.where(Tenant.account_id.is_(None))
    return [_summary(t) for t in db.scalars(query)]


def _free_inbox_address(db: Session, name: str) -> str:
    """The address derived from the name, numbered if another location has it
    ("the-corner-ladle-2@..."): two locations of one chain often share a name."""
    base = inbox_address_for(name)
    local, _, domain = base.partition("@")
    if not local:
        local = "location"
    taken = set(
        db.scalars(select(func.lower(Tenant.inbox_address)).where(Tenant.inbox_address.ilike(f"{local}%@{domain}")))
    )
    candidate, n = f"{local}@{domain}", 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{local}-{n}@{domain}"
    return candidate


@router.post("", response_model=TenantSummary, status_code=201)
def create_tenant(
    body: TenantCreate, db: Session = Depends(get_db), operator: User = Depends(require_operator)
) -> TenantSummary:
    name, metro = body.name.strip(), body.metro.strip()
    if not name or not metro:
        raise HTTPException(status_code=422, detail="enter a name and a metro")
    tenant = Tenant(
        id=uuid.uuid4(),
        name=name,
        metro=metro,
        volume_tier=body.volume_tier,
        inbox_address=_free_inbox_address(db, name),
    )
    db.add(tenant)
    audit.record(
        db,
        operator,
        "tenant.created",
        "tenant",
        tenant.id,
        tenant.id,
        name=name,
        metro=metro,
        volume_tier=body.volume_tier,
        inbox_address=tenant.inbox_address,
    )
    db.commit()
    return _summary(tenant)


@router.get("/{tenant_id}", response_model=TenantSummary)
def get_tenant(tenant_id: uuid.UUID, db: Session = Depends(get_db)) -> TenantSummary:
    return _summary(get_tenant_or_404(db, tenant_id))
