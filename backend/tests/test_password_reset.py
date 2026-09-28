"""'Forgot your password?' (app/password_reset.py, app/api/auth.py): the
emailed link, what using it does, and what it never reveals."""
import re
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE, SESSION_COOKIE
from app.config import settings
from app.main import app
from app.models import AuditEvent, PasswordResetToken, User

from test_digest import PASSWORD, _outbox_files, _tenant, _user, db, outbox  # noqa: F401

pytestmark = pytest.mark.real_auth

CSRF = {CSRF_HEADER: CSRF_HEADER_VALUE}
NEW_PASSWORD = "a brand new passphrase"


def _ask(client, email):
    return client.post("/auth/password-reset/request", json={"email": email}, headers=CSRF)


def _mails_to(outbox, user) -> list:
    return [p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]


def _token_from(path) -> str:
    parsed = message_from_bytes(path.read_bytes(), policy=policy.default)
    text = parsed.get_body(("plain",)).get_content()
    url = re.search(r"https?://\S+/reset-password\?\S+", text).group(0)
    return parse_qs(urlparse(url).query)["token"][0]


def test_the_emailed_link_sets_a_new_password_once_and_signs_you_in(db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    user = _user(db, _tenant(db), password_change_required=True)
    old_browser = TestClient(app)
    assert old_browser.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF).status_code == 200

    client = TestClient(app)
    assert _ask(client, user.email.upper()).status_code == 202  # addresses are case-insensitive
    (mail_path,) = _mails_to(outbox, user)
    parsed = message_from_bytes(mail_path.read_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Reset your Invoice Intelligence password"
    assert "https://app.example.com/reset-password?token=" in parsed.get_body(("html",)).get_content()
    token = _token_from(mail_path)

    assert client.post("/auth/password-reset/check", json={"token": token}, headers=CSRF).status_code == 204
    done = client.post("/auth/password-reset", json={"token": token, "new_password": NEW_PASSWORD}, headers=CSRF)
    assert done.status_code == 200, done.text
    assert done.json()["password_change_required"] is False, "the password is theirs now"
    assert SESSION_COOKIE in client.cookies
    assert client.get("/auth/me").status_code == 200

    # Whoever was signed in before is out, and the old password is dead.
    assert old_browser.get("/auth/me").status_code == 401
    fresh = TestClient(app)
    assert fresh.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF).status_code == 401
    assert fresh.post("/auth/login", json={"email": user.email, "password": NEW_PASSWORD}, headers=CSRF).status_code == 200

    # Once only.
    again = client.post("/auth/password-reset", json={"token": token, "new_password": "yet another passphrase"}, headers=CSRF)
    assert again.status_code == 410
    assert client.post("/auth/password-reset/check", json={"token": token}, headers=CSRF).status_code == 410

    actions = set(db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == user.id)))
    assert {"auth.password_reset_requested", "user.password_reset_by_email", "auth.login"} <= actions


def test_the_answer_is_the_same_whether_or_not_there_is_an_account(db, outbox):
    inactive = _user(db, _tenant(db), is_active=False)
    client = TestClient(app)
    for email in ("nobody-here@test.invalid", inactive.email):
        resp = _ask(client, email)
        assert resp.status_code == 202 and resp.content == b""
    assert _mails_to(outbox, inactive) == []


def test_using_one_link_ends_the_others(db, outbox):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    _ask(client, user.email)
    _ask(client, user.email)
    first, second = (_token_from(p) for p in _mails_to(outbox, user))
    assert client.post("/auth/password-reset", json={"token": second, "new_password": NEW_PASSWORD}, headers=CSRF).status_code == 200
    assert client.post("/auth/password-reset/check", json={"token": first}, headers=CSRF).status_code == 410


def test_an_address_gets_a_few_links_an_hour_at_most(db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "password_reset_max_per_hour", 2)
    user = _user(db, _tenant(db))
    client = TestClient(app)
    for _ in range(4):
        assert _ask(client, user.email).status_code == 202
    assert len(_mails_to(outbox, user)) == 2


def test_an_expired_link_does_nothing(db, outbox):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    _ask(client, user.email)
    token = _token_from(_mails_to(outbox, user)[0])
    db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    db.commit()
    resp = client.post("/auth/password-reset", json={"token": token, "new_password": NEW_PASSWORD}, headers=CSRF)
    assert resp.status_code == 410
    assert "expired" in resp.json()["detail"]


def test_a_too_short_password_is_refused_and_the_link_still_works(db, outbox):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    _ask(client, user.email)
    token = _token_from(_mails_to(outbox, user)[0])
    short = client.post("/auth/password-reset", json={"token": token, "new_password": "short"}, headers=CSRF)
    assert short.status_code == 422 and "at least 12" in short.json()["detail"]
    assert client.post("/auth/password-reset/check", json={"token": token}, headers=CSRF).status_code == 204


def test_it_gets_you_past_a_sign_in_lockout(db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 2)
    user = _user(db, _tenant(db))
    client = TestClient(app)
    for _ in range(2):
        client.post("/auth/login", json={"email": user.email, "password": "wrong guess"}, headers=CSRF)
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF).status_code == 429
    _ask(client, user.email)
    token = _token_from(_mails_to(outbox, user)[0])
    assert client.post("/auth/password-reset", json={"token": token, "new_password": NEW_PASSWORD}, headers=CSRF).status_code == 200
    fresh = TestClient(app)
    assert fresh.post("/auth/login", json={"email": user.email, "password": NEW_PASSWORD}, headers=CSRF).status_code == 200


def test_it_needs_the_request_header_and_can_be_switched_off(db, monkeypatch):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    assert client.post("/auth/password-reset/request", json={"email": user.email}).status_code == 403
    monkeypatch.setattr(settings, "password_reset_enabled", False)
    off = _ask(client, user.email)
    assert off.status_code == 503 and "ask your operator" in off.json()["detail"]
    db.expire_all()
    assert db.scalar(select(PasswordResetToken.id).where(PasswordResetToken.user_id == user.id)) is None
    assert db.get(User, user.id).is_active


def test_changing_the_password_another_way_ends_outstanding_links(db, outbox):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    _ask(client, user.email)
    token = _token_from(_mails_to(outbox, user)[0])
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF).status_code == 200
    changed = client.post(
        "/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD}, headers=CSRF
    )
    assert changed.status_code == 204
    assert client.post("/auth/password-reset/check", json={"token": token}, headers=CSRF).status_code == 410
