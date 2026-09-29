"""'Forgot your password?': a single-use link by email.

The link carries a random token; only its SHA-256 is stored
(PasswordResetToken), as for sessions, so the table can't be read for
working links. It works once, for settings.password_reset_ttl_minutes, and
using it ends every one of that person's links and sessions.

Asking never reveals whether an address has an account: the API answers the
same either way, and does all the work (the lookup, the link, the email)
after the response has gone, so the time taken doesn't give it away either.
Each address gets at most settings.password_reset_max_per_hour links an
hour, counted under a per-address lock so simultaneous requests can't slip
past it, so the form can't be used to flood someone's inbox.
"""
import hashlib
import html
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app import audit
from app import email_design as design
from app import mail
from app.auth import hash_password
from app.config import settings
from app.db import SessionLocal
from app.models import PasswordResetToken, User
from app.users import check_new_password, end_reset_links, revoke_sessions

logger = logging.getLogger(__name__)

REQUESTED = "auth.password_reset_requested"
COMPLETED = "user.password_reset_by_email"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def handle_request(email: str) -> None:
    """Everything a reset request does, run after the response (a background
    task, with its own session): whether or not there's an account, the
    request itself costs the same."""
    db = SessionLocal()
    try:
        issued = request_reset(db, email)
    except Exception:
        logger.exception("password reset request for %s failed", email)
        return
    finally:
        db.close()
    if issued is not None:
        send_reset_email(*issued)


def request_reset(db: Session, email: str) -> tuple[str, str, str] | None:
    """A new link for this address's active user, as (name, email, token);
    None if there's no such active user or they've had their hourly
    allowance. Records the request either way; commits."""
    # One request per address at a time, so the count below can't be read
    # by several at once before any of them has added a link.
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"password-reset|{email}", 0))))
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        audit.record(db, None, REQUESTED, "user", None, email=email, sent=False)
        db.commit()
        return None
    now = datetime.now(timezone.utc)
    recent = db.scalar(
        select(func.count(PasswordResetToken.id)).where(
            PasswordResetToken.user_id == user.id, PasswordResetToken.created_at > now - timedelta(hours=1)
        )
    )
    if recent >= settings.password_reset_max_per_hour:
        audit.record(db, None, REQUESTED, "user", user.id, email=email, sent=False, reason="hourly limit")
        db.commit()
        return None
    _prune(db)
    token = issue_token(db, user)
    audit.record(db, None, REQUESTED, "user", user.id, email=email, sent=True)
    issued = (user.name, user.email, token)
    db.commit()
    return issued


def _prune(db: Session) -> None:
    """Links dead for a while (used or expired) go, as for sessions
    (app.auth.prune_sessions). Run when a link is issued, which is when the
    table grows, and so at most a few times an hour per real account; never
    for a request anyone can make up an address for."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.session_retention_days)
    db.execute(
        delete(PasswordResetToken).where(
            or_(PasswordResetToken.expires_at < cutoff, PasswordResetToken.used_at < cutoff)
        )
    )


def issue_token(db: Session, user: User) -> str:
    """A new link's token (the raw one, for the email; only its hash is kept)."""
    token = secrets.token_urlsafe(32)
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_hash(token),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.password_reset_ttl_minutes),
        )
    )
    return token


def valid_token(db: Session, token: str) -> tuple[PasswordResetToken, User] | None:
    if not token:
        return None
    row = db.execute(
        select(PasswordResetToken, User)
        .join(User, User.id == PasswordResetToken.user_id)
        .where(PasswordResetToken.token_hash == _hash(token))
    ).first()
    if row is None:
        return None
    link, user = row
    if link.used_at is not None or link.expires_at <= datetime.now(timezone.utc) or not user.is_active:
        return None
    return link, user


def complete_reset(db: Session, link: PasswordResetToken, user: User, new_password: str) -> None:
    """Set the new password. Every session ends (whoever might have been in
    the account is out) and every outstanding link stops working. The caller
    commits, and signs this browser in."""
    check_new_password(new_password)
    now = datetime.now(timezone.utc)
    # Claimed first, under a condition, so the same link used twice at once
    # can only set a password once.
    claimed = db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == link.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    if not claimed:
        raise LinkUsedError()
    end_reset_links(db, user)
    user.password_hash = hash_password(new_password)
    user.password_change_required = False
    revoke_sessions(db, user)
    audit.record(db, user, COMPLETED, "user", user.id, email=user.email)


class LinkUsedError(Exception):
    pass


# --- The email ---------------------------------------------------------------


def reset_url(token: str) -> str:
    return f"{settings.public_base_url}/reset-password?{urlencode({'token': token})}"


def send_reset_email(name: str, email: str, token: str) -> None:
    """Run after the response has gone (a background task): failures are
    logged, since there's no one waiting to tell."""
    url = reset_url(token)
    minutes = settings.password_reset_ttl_minutes
    e = html.escape
    text = "\n".join(
        [
            f"Hi {name},",
            "",
            f"Someone (hopefully you) asked to reset the password for {email}.",
            f"Choose a new one here: {url}",
            "",
            f"The link works once, for {minutes} minutes.",
            "If you didn't ask, ignore this email: your password stays as it is.",
        ]
    )
    body = (
        f"<p style='margin:0 0 4px'>Hi {e(name)},</p>"
        f"<p style='margin:0 0 16px'>Someone (hopefully you) asked to reset the password for "
        f"<strong style='overflow-wrap:anywhere'>{e(email)}</strong>.</p>"
        f"<p style='margin:0 0 16px'>{design.button(url, 'Choose a new password')}</p>"
        f"<p style='margin:0;color:{design.MUTED}'>The link works once, for {minutes} minutes.</p>"
    )
    footer = "If you didn&rsquo;t ask for this, ignore this email: your password stays as it is."
    message = mail.build_message(
        to=email,
        subject="Reset your Invoice Intelligence password",
        text=text,
        html=design.document(tag="Password reset", body=body, footer=footer),
    )
    try:
        mail.send(message)
    except mail.MailError as exc:
        logger.warning("password reset email to %s failed: %s", email, exc)
