"""Grouping locations into the business that owns them.

Accounts decide two things that are otherwise silently wrong for a multi-unit
operator: a benchmark cell counts them once rather than once per location
(app/analytics/benchmark.py), and their locations can't corroborate each
other's SKU corrections (app/normalize/matcher.py). Both were shipped before
there was any way to actually create an account, which made them unreachable.

Operators only (require_operator on the whole router), and every change is
in the audit trail. Regrouping matters more than most edits: detaching a
location raises the distinct-business count in every cell it appears in,
which can un-suppress a cell that was suppressed a moment earlier.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.auth import require_operator
from app.db import get_db
from app.models import Account, Tenant, User
from app.schemas.accounts import AccountCreate, AccountDetail, AccountSummary, AttachTenantRequest, TenantSummary

router = APIRouter(prefix="/accounts", tags=["accounts"], dependencies=[Depends(require_operator)])


def _tenant_summary(tenant: Tenant) -> TenantSummary:
    return TenantSummary(
        id=tenant.id,
        name=tenant.name,
        metro=tenant.metro,
        volume_tier=tenant.volume_tier.value,
        account_id=tenant.account_id,
        inbox_address=tenant.inbox_address,
    )


def _get_account_or_404(db: Session, account_id: uuid.UUID) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")
    return account


@router.post("", response_model=AccountDetail, status_code=201)
def create_account(
    payload: AccountCreate, db: Session = Depends(get_db), user: User = Depends(require_operator)
) -> AccountDetail:
    account = Account(id=uuid.uuid4(), name=payload.name.strip())
    db.add(account)
    audit.record(db, user, "account.created", "account", account.id, name=account.name)
    db.commit()
    db.refresh(account)
    return AccountDetail(
        id=account.id, name=account.name, created_at=account.created_at, location_count=0, locations=[]
    )


@router.get("", response_model=list[AccountSummary])
def list_accounts(db: Session = Depends(get_db)) -> list[AccountSummary]:
    # One grouped query for the counts rather than len(account.tenants) per
    # row — the same N+1 rule the rest of the API follows.
    counts = dict(
        db.execute(
            select(Tenant.account_id, func.count(Tenant.id))
            .where(Tenant.account_id.is_not(None))
            .group_by(Tenant.account_id)
        ).all()
    )
    return [
        AccountSummary(
            id=account.id,
            name=account.name,
            created_at=account.created_at,
            location_count=counts.get(account.id, 0),
        )
        for account in db.scalars(select(Account).order_by(Account.name))
    ]


@router.get("/{account_id}", response_model=AccountDetail)
def get_account(account_id: uuid.UUID, db: Session = Depends(get_db)) -> AccountDetail:
    account = _get_account_or_404(db, account_id)
    locations = list(db.scalars(select(Tenant).where(Tenant.account_id == account_id).order_by(Tenant.name)))
    return AccountDetail(
        id=account.id,
        name=account.name,
        created_at=account.created_at,
        location_count=len(locations),
        locations=[_tenant_summary(t) for t in locations],
    )


@router.post("/{account_id}/locations", response_model=AccountDetail)
def attach_location(
    account_id: uuid.UUID,
    payload: AttachTenantRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> AccountDetail:
    account = _get_account_or_404(db, account_id)
    tenant = db.get(Tenant, payload.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    if tenant.account_id is not None and tenant.account_id != account.id:
        # Rejected rather than silently re-parented: moving a location between
        # businesses changes which benchmark cells it can corroborate and
        # whose alias corrections it inherits. Detaching first makes that a
        # deliberate two-step.
        raise HTTPException(
            status_code=409,
            detail=f"tenant already belongs to account {tenant.account_id}; detach it first",
        )

    if tenant.account_id is None:
        tenant.account_id = account.id
        audit.record(
            db, user, "account.location_attached", "account", account.id, tenant.id, account_name=account.name
        )
        db.commit()
    return get_account(account_id, db)


@router.delete("/{account_id}/locations/{tenant_id}", response_model=AccountDetail)
def detach_location(
    account_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_operator),
) -> AccountDetail:
    account = _get_account_or_404(db, account_id)
    tenant = db.get(Tenant, tenant_id)
    if tenant is None or tenant.account_id != account_id:
        raise HTTPException(status_code=404, detail="tenant is not a location of this account")

    tenant.account_id = None
    audit.record(db, user, "account.location_detached", "account", account.id, tenant.id, account_name=account.name)
    db.commit()
    return get_account(account_id, db)
