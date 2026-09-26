"""The distributors an invoice can be attributed to — for the invoice review
screen, when extraction couldn't recognize whose invoice it was."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user
from app.db import get_db
from app.models import Distributor, User
from app.models.distributor import UNRECOGNIZED_SLUG

router = APIRouter(prefix="/distributors", tags=["distributors"])


class DistributorOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


@router.get("", response_model=list[DistributorOut])
def list_distributors(db: Session = Depends(get_db), _: User = Depends(current_user)) -> list[DistributorOut]:
    # 'other' is extraction's "couldn't tell", not a choice a reviewer can make.
    query = select(Distributor).where(Distributor.slug != UNRECOGNIZED_SLUG).order_by(Distributor.name)
    return [DistributorOut(id=d.id, name=d.name, slug=d.slug) for d in db.scalars(query)]
