"""The distributors an invoice can be attributed to: the shared broadline
ones, plus any this business added for itself (a local produce or seafood
vendor; app/business_distributors.py)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, business_distributors
from app.analytics.benchmark import account_key_for
from app.api.deps import get_tenant_or_404
from app.auth import current_user, get_db_for_tenant
from app.models import Distributor, User
from app.models.distributor import UNRECOGNIZED_SLUG

router = APIRouter(prefix="/distributors", tags=["distributors"])


class DistributorOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class NewDistributor(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("", response_model=list[DistributorOut])
def list_distributors(
    tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant), _: User = Depends(current_user)
) -> list[DistributorOut]:
    # 'other' is extraction's "couldn't tell", not a choice a reviewer can make.
    query = (
        select(Distributor)
        .where(Distributor.slug != UNRECOGNIZED_SLUG, business_distributors.visible_to(account_key_for(db, tenant_id)))
        .order_by(Distributor.name)
    )
    return [DistributorOut(id=d.id, name=d.name, slug=d.slug) for d in db.scalars(query)]


@router.post("", response_model=DistributorOut)
def add_distributor(
    tenant_id: uuid.UUID,
    body: NewDistributor,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> DistributorOut:
    """Add a distributor for this business, or return the one it already has
    by that name (or a shared one: "sysco" is Sysco)."""
    get_tenant_or_404(db, tenant_id)
    if not business_distributors.name_key(body.name):
        raise HTTPException(status_code=422, detail="Type the distributor's name.")
    distributor, created = business_distributors.add(db, tenant_id, body.name)
    if created:
        audit.record(db, user, "distributor.added", "distributor", distributor.id, tenant_id, name=distributor.name)
    db.commit()
    return DistributorOut(id=distributor.id, name=distributor.name, slug=distributor.slug)
