"""Signing in and out, and who is signed in."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.auth import (
    CSRF_HEADER,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE,
    accessible_tenant_ids,
    create_session,
    current_user,
    normalize_email,
    prune_sessions,
    revoke_session,
    session_for_token,
    user_for_token,
    verify_password,
)
from app.config import settings
from app.db import get_db
from app.models import AuditEvent, Tenant, User
from app import users as user_service
from app.schemas.auth import LocationOut, LoginRequest, MeOut, PasswordChange

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_FAILED = "auth.login_failed"
LOGIN_SUCCEEDED = "auth.login"


def _me(db: Session, user: User) -> MeOut:
    ids = accessible_tenant_ids(db, user)
    tenants = db.scalars(select(Tenant).where(Tenant.id.in_(ids)).order_by(Tenant.name)) if ids else []
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_operator=user.is_operator,
        password_change_required=user.password_change_required,
        locations=[LocationOut(id=t.id, name=t.name, metro=t.metro) for t in tenants],
    )


def _recent_failures(db: Session, email: str, user: User | None) -> int:
    """Failed sign-ins for this address in the window, counting only those
    since its last successful one: getting the password right clears the slate,
    as it would for a person who mistyped a few times."""
    since = datetime.now(timezone.utc) - timedelta(minutes=settings.login_failure_window_minutes)
    if user is not None:
        last_success = db.scalar(
            select(func.max(AuditEvent.occurred_at)).where(
                AuditEvent.action == LOGIN_SUCCEEDED, AuditEvent.entity_id == user.id
            )
        )
        if last_success is not None and last_success > since:
            since = last_success
    return db.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == LOGIN_FAILED,
            AuditEvent.details["email"].astext == email,
            AuditEvent.occurred_at > since,
        )
    )


@router.post("/login", response_model=MeOut)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> MeOut:
    # The CSRF header is required here even though there's no session cookie
    # yet: otherwise another site could sign a visitor in as the attacker, and
    # whatever they then uploaded or corrected would land in the attacker's
    # account (login CSRF).
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=403, detail="missing request header")

    email = normalize_email(body.email)
    user = db.scalar(select(User).where(User.email == email))
    # Counted per address, from the audit trail itself, so it survives
    # restarts and needs no second store. Checked before verifying, so a
    # locked-out guess costs no Argon2 work, and the same way whether or not
    # the address has an account.
    if _recent_failures(db, email, user) >= settings.login_max_failures:
        raise HTTPException(status_code=429, detail="too many failed sign-ins; try again in a few minutes")

    # verify_password runs even when there's no such user, so both failures
    # take the same time; and both get the same message, so the response
    # doesn't reveal which addresses have accounts.
    if not verify_password(user, body.password) or not user.is_active:
        audit.record(db, None, LOGIN_FAILED, "user", user.id if user else None, email=email)
        db.commit()
        raise HTTPException(status_code=401, detail="email or password is incorrect")

    prune_sessions(db)
    token = create_session(db, user)
    audit.record(db, user, LOGIN_SUCCEEDED, "user", user.id)
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        # The cookie lives as long as the session could (the absolute cap);
        # whether it's still valid, idle expiry included, is the server's call
        # on every request. Refreshing the cookie itself wouldn't work anyway:
        # most pages render on the server, whose responses to its own API
        # calls never reach the browser.
        max_age=settings.session_max_age_days * 24 * 3600,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return _me(db, user)


@router.post("/logout", status_code=204)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        user = user_for_token(db, token)
        revoke_session(db, token)
        if user is not None:
            audit.record(db, user, "auth.logout", "user", user.id)
        db.commit()
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    return _me(db, user)


@router.post("/password", status_code=204)
def change_password(body: PasswordChange, request: Request, db: Session = Depends(get_db)) -> Response:
    """Change your own password. Needs the current one: a session left open on
    a shared machine mustn't be enough to take over the account. Wrong guesses
    count toward the same lockout as failed sign-ins."""
    found = session_for_token(db, request.cookies.get(SESSION_COOKIE))
    if found is None:
        raise HTTPException(status_code=401, detail="sign in required")
    session, user = found
    if _recent_failures(db, user.email, user) >= settings.login_max_failures:
        raise HTTPException(status_code=429, detail="too many failed attempts; try again in a few minutes")
    # 422, not 401: the session is fine, only the typed password is wrong,
    # and a 401 would send the screen to sign-in.
    if not verify_password(user, body.current_password):
        audit.record(db, None, LOGIN_FAILED, "user", user.id, email=user.email, during="password change")
        db.commit()
        raise HTTPException(status_code=422, detail="current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="choose a password different from the current one")
    try:
        user_service.change_own_password(db, user, session.id, body.new_password)
    except user_service.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return Response(status_code=204)
