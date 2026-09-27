"""Sign-in, sessions, and the access rules every endpoint goes through.

Access is decided here and nowhere else. Every location-scoped endpoint
already depends on get_db_for_tenant, so that dependency is where membership
is enforced: an endpoint can't forget to check, because it can't get a tenant
session without passing the check. Cross-tenant screens depend on
require_operator instead.

Sessions are a random token in an HttpOnly cookie, stored server-side as a
SHA-256 hash (UserSession), so they can be revoked immediately and a database
leak doesn't hand out working logins. Passwords are Argon2id.

CSRF: state-changing requests that carry the session cookie must also carry
CSRF_HEADER. A browser only sends a custom header cross-origin after a CORS
preflight, and CORS here admits only the configured frontend origins, so a
page on any other origin can't forge a request that rides the cookie.
SameSite=Lax alone isn't enough: it treats every port on the same host as the
same site, and anything else running on localhost would qualify.
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.db import bind_tenant, get_db
from app.models import Tenant, TenantMembership, User, UserSession

SESSION_COOKIE = "ii_session"
CSRF_HEADER = "X-Requested-With"
CSRF_HEADER_VALUE = "invoice-intelligence"

_hasher = PasswordHasher()
# Verified against when the email doesn't exist, so "no such user" and "wrong
# password" take the same time and can't be told apart by timing.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(user: User | None, password: str) -> bool:
    try:
        return _hasher.verify(user.password_hash if user else _DUMMY_HASH, password) and user is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User) -> str:
    """Returns the raw token for the cookie. Only its hash is stored."""
    token = secrets.token_urlsafe(32)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=_token_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(days=settings.session_ttl_days),
        )
    )
    return token


# Extending a session is a write; doing it at most this often per session
# keeps page views from each costing one.
SESSION_REFRESH_GRANULARITY = timedelta(hours=1)


def _extend(db: Session, session: UserSession) -> None:
    """Push the idle expiry back, never past the absolute cap. Committed on
    its own: most requests that use a session are reads that never commit."""
    now = datetime.now(timezone.utc)
    target = min(
        now + timedelta(days=settings.session_ttl_days),
        session.created_at + timedelta(days=settings.session_max_age_days),
    )
    if target - session.expires_at >= SESSION_REFRESH_GRANULARITY:
        db.execute(update(UserSession).where(UserSession.id == session.id).values(expires_at=target))
        db.commit()


def prune_sessions(db: Session) -> int:
    """Delete sessions that stopped working more than session_retention_days
    ago (expired or revoked). Run on every sign-in, which is exactly when the
    table grows, so it never needs a scheduler. Indexed (migration 0010)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.session_retention_days)
    return db.execute(
        delete(UserSession).where(or_(UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff))
    ).rowcount


def revoke_session(db: Session, token: str) -> None:
    session = db.scalar(select(UserSession).where(UserSession.token_hash == _token_hash(token)))
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)


def session_for_token(db: Session, token: str | None) -> tuple[UserSession, User] | None:
    """The live session a token belongs to, and its user; None when there's
    no such session or it has ended. Using it extends it (_extend)."""
    if not token:
        return None
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == _token_hash(token))
    ).first()
    if row is None:
        return None
    session, user = row
    if session.revoked_at is not None or session.expires_at <= datetime.now(timezone.utc) or not user.is_active:
        return None
    _extend(db, session)
    return session, user


def user_for_token(db: Session, token: str | None) -> User | None:
    found = session_for_token(db, token)
    return found[1] if found else None


PASSWORD_CHANGE_REQUIRED = "password change required"
# What someone holding an operator-issued password may do before replacing
# it: see who they are, replace it, or leave.
_ALLOWED_BEFORE_PASSWORD_CHANGE = {"/auth/me", "/auth/password", "/auth/logout"}


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_for_token(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    # Enforced here, not in the screens: every endpoint that acts as a user
    # comes through this dependency (get_db_for_tenant and require_operator
    # included), so none can be reached with a password someone else has seen.
    if user.password_change_required and request.url.path not in _ALLOWED_BEFORE_PASSWORD_CHANGE:
        raise HTTPException(status_code=403, detail=PASSWORD_CHANGE_REQUIRED)
    return user


def require_operator(user: User = Depends(current_user)) -> User:
    if not user.is_operator:
        raise HTTPException(status_code=403, detail="operator access required")
    return user


def accessible_tenant_ids(db: Session, user: User) -> list[uuid.UUID]:
    if user.is_operator:
        return list(db.scalars(select(Tenant.id).order_by(Tenant.name)))
    return list(db.scalars(select(TenantMembership.tenant_id).where(TenantMembership.user_id == user.id)))


def can_access_tenant(db: Session, user: User, tenant_id: uuid.UUID) -> bool:
    if user.is_operator:
        return True
    return (
        db.scalar(
            select(TenantMembership.id).where(
                TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant_id
            )
        )
        is not None
    )


def get_db_for_tenant(
    tenant_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
) -> Session:
    """FastAPI dependency: the signed-in user's session, scoped to tenant_id.

    Refuses with 404, not 403, for a location the user has no access to, so
    the response doesn't confirm that another business's location exists.
    Shares get_db's session with current_user (FastAPI caches dependencies per
    request), so authorizing costs no second connection.
    """
    if not can_access_tenant(db, user, tenant_id):
        raise HTTPException(status_code=404, detail="tenant not found")
    bind_tenant(db, tenant_id)
    return db


def csrf_ok(request: Request) -> bool:
    """See the module docstring. Only requests riding the session cookie can be
    forged into acting as a user, so only those need the header."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return True
    if SESSION_COOKIE not in request.cookies:
        return True
    return request.headers.get(CSRF_HEADER) == CSRF_HEADER_VALUE
