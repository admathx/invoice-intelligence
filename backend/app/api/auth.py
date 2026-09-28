"""Signing in and out, and who is signed in."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, password_reset
from app.auth import (
    CSRF_HEADER,
    CSRF_HEADER_VALUE,
    SESSION_COOKIE,
    accessible_tenant_ids,
    create_session,
    normalize_email,
    prune_sessions,
    current_user,
    revoke_session,
    rotate_session,
    session_for_token,
    signed_in_user,
    user_for_token,
    verify_password,
)
from app.config import settings
from app.db import get_db
from app.models import AuditEvent, Tenant, User
from app import users as user_service
from app.schemas.auth import (
    AccountUpdate,
    LocationOut,
    LoginRequest,
    MeOut,
    PasswordChange,
    PasswordResetCheck,
    PasswordResetComplete,
    PasswordResetRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

LOGIN_FAILED = "auth.login_failed"
LOGIN_SUCCEEDED = "auth.login"
# A signed-in user entering the wrong current password to change it. Counted
# with failed sign-ins toward the lockout (both are guesses at the password),
# but recorded as what it was, and by whom.
PASSWORD_CHANGE_FAILED = "auth.password_change_failed"
_PASSWORD_GUESSES = (LOGIN_FAILED, PASSWORD_CHANGE_FAILED)


def _me(db: Session, user: User) -> MeOut:
    ids = accessible_tenant_ids(db, user)
    tenants = db.scalars(select(Tenant).where(Tenant.id.in_(ids)).order_by(Tenant.name)) if ids else []
    return MeOut(
        id=user.id,
        email=user.email,
        name=user.name,
        is_operator=user.is_operator,
        password_change_required=user.password_change_required,
        digest_enabled=user.digest_enabled,
        alert_emails_enabled=user.alert_emails_enabled,
        alert_email_min_pct_change=settings.alert_email_min_pct_change,
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
            AuditEvent.action.in_(_PASSWORD_GUESSES),
            AuditEvent.details["email"].astext == email,
            AuditEvent.occurred_at > since,
        )
    )


def _set_session_cookie(response: Response, token: str) -> None:
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


def _require_csrf_header(request: Request) -> None:
    """For the signed-out forms (sign in, password reset), which can't rely on
    csrf_ok's cookie test: there's no session cookie yet. Without it another
    site could sign a visitor in as the attacker, so whatever they then
    uploaded or corrected would land in the attacker's account (login CSRF),
    or make visitors' browsers request reset emails for whomever it liked."""
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        raise HTTPException(status_code=403, detail="missing request header")


@router.post("/login", response_model=MeOut)
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)) -> MeOut:
    _require_csrf_header(request)

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
    _set_session_cookie(response, token)
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
def me(user: User = Depends(signed_in_user), db: Session = Depends(get_db)) -> MeOut:
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
        audit.record(db, user, PASSWORD_CHANGE_FAILED, "user", user.id, email=user.email)
        db.commit()
        raise HTTPException(status_code=422, detail="current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=422, detail="choose a password different from the current one")
    try:
        user_service.change_own_password(db, user, session.id, body.new_password)
    except user_service.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # This browser stays signed in, on a new token: a copy of the old cookie
    # (the shared-PC case the current-password check is for) stops working
    # along with every other session.
    token = rotate_session(db, session)
    db.commit()
    response = Response(status_code=204)
    _set_session_cookie(response, token)
    return response


@router.patch("/me", response_model=MeOut)
def update_me(body: AccountUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)) -> MeOut:
    """Your own preferences: which emails you get."""
    changed = False
    for setting, (on, off) in _EMAIL_SETTINGS.items():
        wanted = getattr(body, setting)
        if wanted is not None and wanted != getattr(user, setting):
            setattr(user, setting, wanted)
            audit.record(db, user, on if wanted else off, "user", user.id, email=user.email)
            changed = True
    if changed:
        db.commit()
    return _me(db, user)


# Each email preference, and the audit actions for turning it on and off.
_EMAIL_SETTINGS = {
    "digest_enabled": ("user.digest_subscribed", "user.digest_unsubscribed"),
    "alert_emails_enabled": ("user.alert_emails_subscribed", "user.alert_emails_unsubscribed"),
}


# --- Forgot your password? (app/password_reset.py) ----------------------------

_RESET_OFF = "password reset by email isn't set up here; ask your operator to reset your password"
_LINK_INVALID = "this link has expired or has already been used; ask for a new one"


def _reset_enabled() -> None:
    if not settings.password_reset_enabled:
        raise HTTPException(status_code=503, detail=_RESET_OFF)


@router.post("/password-reset/request", status_code=202)
def request_password_reset(
    body: PasswordResetRequest, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)
) -> Response:
    """Always the same answer, whether or not the address has an account; the
    email, if there is one, goes out after the response."""
    _require_csrf_header(request)
    _reset_enabled()
    issued = password_reset.request_reset(db, normalize_email(body.email))
    if issued is not None:
        user, token = issued
        background.add_task(password_reset.send_reset_email, user.name, user.email, token)
    return Response(status_code=202)


@router.post("/password-reset/check", status_code=204)
def check_password_reset(body: PasswordResetCheck, request: Request, db: Session = Depends(get_db)) -> Response:
    """Whether a link still works, so the page can say so before anyone types
    a new password into it."""
    _require_csrf_header(request)
    _reset_enabled()
    if password_reset.valid_token(db, body.token) is None:
        raise HTTPException(status_code=410, detail=_LINK_INVALID)
    return Response(status_code=204)


@router.post("/password-reset", response_model=MeOut)
def complete_password_reset(
    body: PasswordResetComplete, request: Request, response: Response, db: Session = Depends(get_db)
) -> MeOut:
    """Set a new password from an emailed link, and sign this browser in: the
    link has just proved the email is theirs. Every other session ends."""
    _require_csrf_header(request)
    _reset_enabled()
    found = password_reset.valid_token(db, body.token)
    if found is None:
        raise HTTPException(status_code=410, detail=_LINK_INVALID)
    link, user = found
    try:
        password_reset.complete_reset(db, link, user, body.new_password)
    except user_service.UserError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except password_reset.LinkUsedError as exc:
        db.rollback()
        raise HTTPException(status_code=410, detail=_LINK_INVALID) from exc
    token = create_session(db, user)
    # A sign-in like any other: it also clears the failed-attempt count, so
    # someone who locked themselves out can get straight back in.
    audit.record(db, user, LOGIN_SUCCEEDED, "user", user.id, via="password reset")
    db.commit()
    _set_session_cookie(response, token)
    return _me(db, user)
