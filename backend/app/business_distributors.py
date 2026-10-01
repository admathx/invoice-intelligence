"""Distributors a business adds for itself: the local produce company, the
seafood supplier, the cash-and-carry. The shared list only has the big
broadline distributors, and an invoice that couldn't be attributed to one of
them could never be matched or priced, so a quarter of a typical kitchen's
buying went untracked.

Each is visible only to the business that added it, and is recognized by name
on its next invoice. A business is its account, or a location with no
account (account_key_column). A location sees what it added itself and what
its account added: its own vendors stay when it later joins an account.
"""
import re
import uuid

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.analytics.benchmark import account_key_for
from app.models import Distributor, Tenant
from app.models.distributor import UNRECOGNIZED_SLUG

# Words that don't tell two vendors apart ("&" drops out as punctuation, so
# "and" does too): "FreshLine Produce Co." and
# "Freshline Produce Company, Inc" are the same one.
_NOISE = re.compile(r"\b(INC|INCORPORATED|LLC|LTD|CO|COMPANY|CORP|CORPORATION|THE|AND)\b")


def name_words(name: str) -> list[str]:
    """A name's words, without punctuation, case or company suffixes."""
    return _NOISE.sub(" ", re.sub(r"[^A-Z0-9 ]+", " ", name.upper())).split()


def name_key(name: str) -> str:
    """A name reduced to what identifies it: letters and digits, no
    punctuation, case or company suffixes. Stored as Distributor.name_key."""
    return "".join(name_words(name))


def _keys(db: Session, tenant_id: uuid.UUID) -> list[uuid.UUID]:
    """Whose distributors this location sees: its own, and its account's."""
    account_id = db.scalar(select(Tenant.account_id).where(Tenant.id == tenant_id))
    return [tenant_id] if account_id is None else [tenant_id, account_id]


def visible_to(db: Session, tenant_id: uuid.UUID):
    """The shared distributors plus this location's business's own."""
    return or_(Distributor.account_key.is_(None), Distributor.account_key.in_(_keys(db, tenant_id)))


def find_by_name(db: Session, tenant_id: uuid.UUID, name: str | None) -> Distributor | None:
    """The distributor this location can use whose name is `name`, if any.
    A shared one first, then the business's own. Never extraction's 'other',
    which isn't a distributor."""
    key = name_key(name or "")
    if not key:
        return None
    return db.scalar(
        select(Distributor)
        .where(Distributor.name_key == key, visible_to(db, tenant_id), Distributor.slug != UNRECOGNIZED_SLUG)
        .order_by(Distributor.account_key.is_not(None), Distributor.name)
        .limit(1)
    )


def add(db: Session, tenant_id: uuid.UUID, name: str) -> tuple[Distributor, bool]:
    """This business's distributor called `name`, and whether it's new: an
    existing one with that name (theirs or a shared one) or a new one. Not
    committed."""
    name = " ".join(name.split())
    existing = find_by_name(db, tenant_id, name)
    if existing is not None:
        return existing, False
    slug = f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:40]}-{uuid.uuid4().hex[:8]}"
    distributor = Distributor(
        id=uuid.uuid4(), name=name, slug=slug, name_key=name_key(name), account_key=account_key_for(db, tenant_id)
    )
    try:
        with db.begin_nested():
            db.add(distributor)
    except IntegrityError:
        # Someone else in the business added it a moment ago.
        return find_by_name(db, tenant_id, name), False
    return distributor, True


def usable_by(db: Session, tenant_id: uuid.UUID, distributor: Distributor) -> bool:
    return distributor.account_key is None or distributor.account_key in _keys(db, tenant_id)
