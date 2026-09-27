"""Managing logins: the one implementation behind both the operators' Users
screen (app/api/users.py) and the command line (scripts/manage_users.py).

Every function records its change in the audit trail and leaves committing to
the caller, so the change and its record land together (app.audit). `actor`
is the operator making the change, or None from the command line, where
nobody is signed in.
"""
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app import audit
from app.auth import hash_password, normalize_email
from app.models import Tenant, TenantMembership, User, UserSession

MIN_PASSWORD_LENGTH = 12


class UserError(ValueError):
    """A change that can't be made, with a message fit to show the operator."""

    def __init__(self, message: str, *, conflict: bool = False):
        super().__init__(message)
        self.conflict = conflict


def generate_password() -> str:
    """A temporary password for an operator to hand over. 16 URL-safe
    characters: typeable, and ~96 bits, far beyond guessing under the
    sign-in throttle."""
    return secrets.token_urlsafe(12)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def revoke_sessions(db: Session, user: User) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


def _tenants(db: Session, tenant_ids: list[uuid.UUID]) -> list[Tenant]:
    tenants = list(db.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids)))) if tenant_ids else []
    missing = set(tenant_ids) - {t.id for t in tenants}
    if missing:
        raise UserError(f"no such location: {', '.join(sorted(map(str, missing)))}")
    return tenants


def create_user(
    db: Session,
    actor: User | None,
    *,
    email: str,
    name: str,
    password: str,
    is_operator: bool = False,
    tenant_ids: list[uuid.UUID] = (),
) -> User:
    email = normalize_email(email)
    name = name.strip()
    if "@" not in email:
        raise UserError("enter an email address")
    if not name:
        raise UserError("enter a name")
    _check_password(password)
    if db.scalar(select(User.id).where(User.email == email)):
        raise UserError(f"{email} already has a login", conflict=True)
    tenants = _tenants(db, list(tenant_ids))

    user = User(
        id=uuid.uuid4(), email=email, name=name, password_hash=hash_password(password), is_operator=is_operator
    )
    db.add(user)
    db.flush()
    audit.record(db, actor, "user.created", "user", user.id, email=email, name=name, is_operator=is_operator)
    for tenant in tenants:
        _grant(db, actor, user, tenant)
    return user


def _grant(db: Session, actor: User | None, user: User, tenant: Tenant) -> bool:
    exists = db.scalar(
        select(TenantMembership.id).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant.id)
    )
    if exists:
        return False
    db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id))
    audit.record(db, actor, "user.access_granted", "user", user.id, tenant.id, email=user.email, location=tenant.name)
    return True


def grant_access(db: Session, actor: User | None, user: User, tenant_id: uuid.UUID) -> bool:
    """False if they already had it."""
    (tenant,) = _tenants(db, [tenant_id])
    return _grant(db, actor, user, tenant)


def revoke_access(db: Session, actor: User | None, user: User, tenant_id: uuid.UUID) -> bool:
    """False if they didn't have it."""
    (tenant,) = _tenants(db, [tenant_id])
    removed = db.execute(
        delete(TenantMembership).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant.id)
    ).rowcount
    if removed:
        audit.record(db, actor, "user.access_revoked", "user", user.id, tenant.id, email=user.email, location=tenant.name)
    return bool(removed)


def update_user(
    db: Session,
    actor: User | None,
    user: User,
    *,
    name: str | None = None,
    is_operator: bool | None = None,
    is_active: bool | None = None,
) -> dict:
    """Returns what changed. An operator can't demote or deactivate
    themselves: done by accident, there may be no operator left to undo it."""
    if actor is not None and actor.id == user.id and (is_operator is False or is_active is False):
        raise UserError("you can't remove your own operator access or deactivate yourself; ask another operator")
    before = {"name": user.name, "is_operator": user.is_operator, "is_active": user.is_active}
    if name is not None:
        if not name.strip():
            raise UserError("enter a name")
        user.name = name.strip()
    if is_operator is not None:
        user.is_operator = is_operator
    if is_active is not None:
        user.is_active = is_active
    changed = audit.changes(before, {"name": user.name, "is_operator": user.is_operator, "is_active": user.is_active})
    if changed:
        if is_active is False and before["is_active"]:
            # Signed out everywhere now, not at the session's natural expiry.
            revoke_sessions(db, user)
        audit.record(db, actor, "user.updated", "user", user.id, email=user.email, changes=changed)
    return changed


def set_password(db: Session, actor: User | None, user: User, password: str) -> None:
    """Also signs the user out everywhere: whoever knew the old password, or
    holds a session opened with it, is out."""
    _check_password(password)
    user.password_hash = hash_password(password)
    revoke_sessions(db, user)
    audit.record(db, actor, "user.password_reset", "user", user.id, email=user.email)
