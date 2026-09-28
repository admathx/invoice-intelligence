"""Unsubscribing from the weekly digest or the price-increase alerts, from
the link in the email (`kind`: "digest", the default, or "alerts").

No sign-in: the link carries the user id and an HMAC of it
(app.digest.unsubscribe_token), which is the authorization. GET shows a page
with a button rather than unsubscribing at once, because mail security
scanners open every link in a message, and a GET that acted would
unsubscribe people who never clicked. POST does it: from that button, or
straight from a mail client's own unsubscribe button (RFC 8058 one-click,
advertised in the message's List-Unsubscribe-Post header).
"""
import html
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.db import get_db
from app.digest import valid_unsubscribe_token
from app.models import User

router = APIRouter(prefix="/digest", tags=["digest"])


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title></head>"
        "<body style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:480px;margin:64px auto;"
        f"padding:0 16px;color:#111827'><h1 style='font-size:20px'>{html.escape(title)}</h1>{body}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _user(db: Session, u: str, t: str, kind: str) -> User | None:
    try:
        user_id = uuid.UUID(u)
    except ValueError:
        return None
    if not valid_unsubscribe_token(user_id, t, kind):
        return None
    return db.get(User, user_id)


# What each kind of email is called on these pages, and the user setting it is.
_LISTS = {
    "digest": ("the weekly email", "digest_enabled", "user.digest_unsubscribed"),
    "alerts": ("price-increase emails", "alert_emails_enabled", "user.alert_emails_unsubscribed"),
}

_INVALID = ("This link isn't valid", "<p>It may have been copied incompletely. You can turn these emails off from your account page instead.</p>")


@router.get("/unsubscribe", response_class=HTMLResponse)
def confirm_unsubscribe(u: str = "", t: str = "", kind: str = "digest", db: Session = Depends(get_db)) -> HTMLResponse:
    user = _user(db, u, t, kind)
    if user is None:
        return _page(*_INVALID, status=404)
    name, setting, _ = _LISTS[kind]
    if not getattr(user, setting):
        return _page("You're already unsubscribed", f"<p>You won't get {name}.</p>")
    return _page(
        f"Stop {name}?",
        f"<p>For {html.escape(user.email)}. You can turn them back on from your account page any time.</p>"
        # The form posts back to this same URL, token and kind included.
        "<form method='post'><button type='submit' style='padding:8px 16px;font-size:14px'>Unsubscribe</button></form>",
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(u: str = "", t: str = "", kind: str = "digest", db: Session = Depends(get_db)) -> HTMLResponse:
    user = _user(db, u, t, kind)
    if user is None:
        return _page(*_INVALID, status=404)
    name, setting, action = _LISTS[kind]
    if getattr(user, setting):
        setattr(user, setting, False)
        audit.record(db, user, action, "user", user.id, email=user.email)
        db.commit()
    account = f"{settings.public_base_url}/account/password"
    return _page(
        "Unsubscribed",
        f"<p>You won't get {name} any more. "
        f"Changed your mind? Turn them back on from <a href='{html.escape(account)}'>your account</a>.</p>",
    )
