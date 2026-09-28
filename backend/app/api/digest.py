"""Unsubscribing from the weekly digest, from the link in the email.

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


def _user(db: Session, u: str, t: str) -> User | None:
    try:
        user_id = uuid.UUID(u)
    except ValueError:
        return None
    if not valid_unsubscribe_token(user_id, t):
        return None
    return db.get(User, user_id)


_INVALID = ("This link isn't valid", "<p>It may have been copied incompletely. You can turn the weekly email off from your account page instead.</p>")


@router.get("/unsubscribe", response_class=HTMLResponse)
def confirm_unsubscribe(u: str = "", t: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    user = _user(db, u, t)
    if user is None:
        return _page(*_INVALID, status=404)
    if not user.digest_enabled:
        return _page("You're already unsubscribed", "<p>You won't get the weekly email.</p>")
    return _page(
        "Stop the weekly email?",
        f"<p>For {html.escape(user.email)}. You can turn it back on from your account page any time.</p>"
        # The form posts back to this same URL, token included.
        "<form method='post'><button type='submit' style='padding:8px 16px;font-size:14px'>Unsubscribe</button></form>",
    )


@router.post("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(u: str = "", t: str = "", db: Session = Depends(get_db)) -> HTMLResponse:
    user = _user(db, u, t)
    if user is None:
        return _page(*_INVALID, status=404)
    if user.digest_enabled:
        user.digest_enabled = False
        audit.record(db, user, "user.digest_unsubscribed", "user", user.id, email=user.email)
        db.commit()
    account = f"{settings.public_base_url}/account/password"
    return _page(
        "Unsubscribed",
        "<p>You won't get the weekly email any more. "
        f"Changed your mind? Turn it back on from <a href='{html.escape(account)}'>your account</a>.</p>",
    )
