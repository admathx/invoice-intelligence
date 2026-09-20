"""Listing tenants, so an operator can pick which locations form a business.

Exists for the accounts UI (app/api/accounts.py) — every other tenant-facing
endpoint takes the tenant as a parameter and never needs to enumerate them.
Same v0 no-auth caveat as accounts.py: this returns every tenant in the system.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.db import get_db
from app.models import Tenant
from app.schemas.accounts import TenantSummary

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("", response_model=list[TenantSummary])
def list_tenants(unassigned: bool = False, db: Session = Depends(get_db)) -> list[TenantSummary]:
    """`unassigned=true` returns only tenants not yet part of a business —
    the candidates for attaching to an account.
    """
    query = select(Tenant).order_by(Tenant.name)
    if unassigned:
        query = query.where(Tenant.account_id.is_(None))
    return [
        TenantSummary(
            id=t.id, name=t.name, metro=t.metro, volume_tier=t.volume_tier.value, account_id=t.account_id
        )
        for t in db.scalars(query)
    ]


@router.get("/{tenant_id}", response_model=TenantSummary)
def get_tenant(tenant_id: uuid.UUID, db: Session = Depends(get_db)) -> TenantSummary:
    tenant = get_tenant_or_404(db, tenant_id)
    return TenantSummary(
        id=tenant.id,
        name=tenant.name,
        metro=tenant.metro,
        volume_tier=tenant.volume_tier.value,
        account_id=tenant.account_id,
    )
