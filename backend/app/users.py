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
from app.models import PasswordResetToken, Tenant, TenantMembership, User, UserSession

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


def check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise UserError(f"Passwords need at least {MIN_PASSWORD_LENGTH} characters.")


def revoke_sessions(db: Session, user: User) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


def end_reset_links(db: Session, user: User) -> None:
    """A password set any other way ends the emailed "forgot your password"
    links still outstanding (app/password_reset.py): they were for replacing
    the password that just went."""
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc))
    )


def _tenants(db: Session, tenant_ids: list[uuid.UUID]) -> list[Tenant]:
    tenants = list(db.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids)))) if tenant_ids else []
    missing = set(tenant_ids) - {t.id for t in tenants}
    if missing:
        raise UserError("That location doesn't exist.")
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
    must_change: bool = False,
) -> User:
    """must_change: the password was issued by someone else (an operator on
    the Users screen), so its owner has to replace it before anything else."""
    email = normalize_email(email)
    name = name.strip()
    if "@" not in email:
        raise UserError("Enter an email address.")
    if not name:
        raise UserError("Enter a name.")
    check_new_password(password)
    if db.scalar(select(User.id).where(User.email == email)):
        raise UserError(f"{email} already has a login.", conflict=True)
    tenants = _tenants(db, list(tenant_ids))

    user = User(
        id=uuid.uuid4(),
        email=email,
        name=name,
        password_hash=hash_password(password),
        is_operator=is_operator,
        password_change_required=must_change,
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
        raise UserError("You can't remove your own admin access or deactivate yourself. Ask another admin.")
    before = {"name": user.name, "is_operator": user.is_operator, "is_active": user.is_active}
    if name is not None:
        if not name.strip():
            raise UserError("Enter a name.")
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


def set_password(db: Session, actor: User | None, user: User, password: str, *, must_change: bool = False) -> None:
    """Also signs the user out everywhere: whoever knew the old password, or
    holds a session opened with it, is out. must_change as in create_user."""
    check_new_password(password)
    user.password_hash = hash_password(password)
    user.password_change_required = must_change
    revoke_sessions(db, user)
    end_reset_links(db, user)
    audit.record(db, actor, "user.password_reset", "user", user.id, email=user.email)


def change_own_password(db: Session, user: User, current_session_id: uuid.UUID, new_password: str) -> None:
    """The owner choosing their own password (the current one already
    verified by the caller). Their other sessions end; the one they're using
    stays, so changing a password doesn't sign you out of the page you did
    it on."""
    check_new_password(new_password)
    user.password_hash = hash_password(new_password)
    user.password_change_required = False
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.id != current_session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    end_reset_links(db, user)
    audit.record(db, user, "user.password_changed", "user", user.id, email=user.email)
