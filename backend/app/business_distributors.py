"""Distributors a business adds for itself: the local produce company, the
seafood supplier, the cash-and-carry. The shared list only has the big
broadline distributors, and an invoice that couldn't be attributed to one of
them could never be matched or priced, so a quarter of a typical kitchen's
buying went untracked.

Each is visible only to the business that added it (its account, or the
location itself: account_key_column), and is recognized by name on its next
invoice.
"""
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.analytics.benchmark import account_key_for
from app.models import Distributor
from app.models.distributor import UNRECOGNIZED_SLUG

# Words that don't tell two vendors apart: "FreshLine Produce Co." and
# "Freshline Produce Company, Inc" are the same one.
_NOISE = re.compile(r"\b(INC|INCORPORATED|LLC|LTD|CO|COMPANY|CORP|CORPORATION|THE)\b")


def name_key(name: str) -> str:
    """A name reduced to what identifies it: letters and digits, no
    punctuation, case or company suffixes."""
    words = _NOISE.sub(" ", re.sub(r"[^A-Z0-9 ]+", " ", name.upper()))
    return "".join(words.split())


def visible_to(account_key: uuid.UUID):
    """The shared distributors plus this business's own."""
    return or_(Distributor.account_key.is_(None), Distributor.account_key == account_key)


def find_by_name(db: Session, tenant_id: uuid.UUID, name: str | None) -> Distributor | None:
    """The distributor this location can use whose name is `name`, if any.
    Never extraction's 'other', which isn't a distributor."""
    key = name_key(name or "")
    if not key:
        return None
    candidates = db.scalars(
        select(Distributor).where(visible_to(account_key_for(db, tenant_id)), Distributor.slug != UNRECOGNIZED_SLUG)
    )
    return next((d for d in candidates if name_key(d.name) == key), None)


def add(db: Session, tenant_id: uuid.UUID, name: str) -> tuple[Distributor, bool]:
    """This business's distributor called `name`, and whether it's new: an
    existing one with that name (theirs or a shared one) or a new one. Not
    committed."""
    name = " ".join(name.split())
    existing = find_by_name(db, tenant_id, name)
    if existing is not None:
        return existing, False
    slug = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]}-{uuid.uuid4().hex[:8]}"
    distributor = Distributor(id=uuid.uuid4(), name=name, slug=slug, account_key=account_key_for(db, tenant_id))
    db.add(distributor)
    db.flush()
    return distributor, True


def usable_by(db: Session, tenant_id: uuid.UUID, distributor: Distributor) -> bool:
    return distributor.account_key is None or distributor.account_key == account_key_for(db, tenant_id)
