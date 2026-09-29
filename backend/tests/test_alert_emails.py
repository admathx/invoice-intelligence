"""Price-increase emails (app/alert_emails.py): which alerts, to whom, once
each, and turning them off without turning off the digest."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email import message_from_bytes, policy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import alert_emails, digest, mail
from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import AlertEmailSend, AuditEvent, PriceAlert, User
from app.models.enums import AlertStatus, AlertType

# The digest's fixtures: committed users, locations and products, cleaned up after.
from test_digest import PASSWORD, _outbox_files, _sku, _tenant, _user, db, outbox  # noqa: F401

pytestmark = pytest.mark.real_auth

# Years ahead: only alerts these tests make fall inside the lookback, never
# the development database's own, which would otherwise be emailed and recorded.
NOW = datetime(2031, 3, 3, 15, 0, tzinfo=timezone.utc)


def _alert(db, tenant, sku, *, pct="0.24", created_at=NOW - timedelta(hours=1), status=AlertStatus.open) -> PriceAlert:
    alert = PriceAlert(
        tenant_id=tenant.id,
        canonical_sku_id=sku.id,
        alert_type=AlertType.creep,
        baseline_price=Decimal("0.54"),
        current_price=Decimal("0.67"),
        pct_change=Decimal(pct),
        window_start=date(2026, 8, 1),
        window_end=date(2026, 9, 27),
        status=status,
        created_at=created_at,
    )
    db.add(alert)
    db.commit()
    return alert


def _send(now):
    """As the scheduler does it: its own plain session. The fixture's session
    never expires what it loaded, which hid a crash that only a default
    session (expire on commit) hits."""
    session = SessionLocal()
    try:
        return alert_emails.send_alert_emails(session, now)
    finally:
        session.close()


def _read(path):
    parsed = message_from_bytes(path.read_bytes(), policy=policy.default)
    return parsed, parsed.get_body(("plain",)).get_content(), parsed.get_body(("html",)).get_content()


def _mine(db, run_users) -> set:
    """Recipients this test made (the dev database may have its own alerts)."""
    return {u for u in run_users if u in db.info["made"]["users"]}


def test_big_new_increases_go_to_members_who_want_them(db, outbox, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    tenant = _tenant(db)
    big = _alert(db, tenant, _sku(db, "Cilantro"))
    _alert(db, tenant, _sku(db, "Small"), pct="0.06")  # the digest's job
    _alert(db, tenant, _sku(db, "Old"), created_at=NOW - timedelta(days=3))  # before the lookback
    _alert(db, tenant, _sku(db, "Resolved"), status=AlertStatus.resolved)
    member = _user(db, tenant)
    opted_out = _user(db, tenant, alert_emails_enabled=False)
    _user(db, tenant, is_active=False)
    _user(db, operator=True)  # sees every location, belongs to none

    _send(NOW)
    sent_to = set(db.scalars(select(AlertEmailSend.user_id).where(AlertEmailSend.alert_id == big.id)))
    assert _mine(db, sent_to) == {member.id}
    assert opted_out.id not in sent_to

    (path,) = [p for p in _outbox_files(outbox) if member.email.replace("@", "_at_") in p.name]
    parsed, text, html_body = _read(path)
    assert parsed["Subject"].startswith(f"Price increase at {tenant.name}: Cilantro")
    assert parsed["Subject"].endswith("up 24.0%")
    assert "Small" not in text and "Old" not in text and "Resolved" not in text
    assert "$0.54/lb -> $0.67/lb (+24.0%)" in text
    assert f"https://app.example.com/insights?location={tenant.id}" in html_body
    assert "kind=alerts" in parsed["List-Unsubscribe"]


def test_each_increase_is_emailed_once_and_several_share_one_email(db, outbox):
    first, second = _tenant(db, "First"), _tenant(db, "Second")
    a = _alert(db, first, _sku(db))
    b = _alert(db, second, _sku(db))
    user = _user(db, first, second)

    _send(NOW)
    _send(NOW + timedelta(minutes=5))
    mine = [p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]
    assert len(mine) == 1
    parsed, _, _ = _read(mine[0])
    assert parsed["Subject"] == "2 price increases at 2 locations"
    claimed = set(db.scalars(select(AlertEmailSend.alert_id).where(AlertEmailSend.user_id == user.id)))
    assert claimed == {a.id, b.id}


def test_a_run_racing_another_sends_only_what_it_claimed(db):
    tenant = _tenant(db)
    alert = _alert(db, tenant, _sku(db))
    user = _user(db, tenant)
    other = SessionLocal()
    try:
        assert alert_emails._claim(db, user.id, [alert.id], NOW) == {alert.id}
        assert alert_emails._claim(other, user.id, [alert.id], NOW) == set()
    finally:
        other.close()


def test_a_product_isnt_emailed_again_within_the_week_when_its_alert_reopens(db, outbox):
    tenant, sku = _tenant(db), _sku(db)
    user = _user(db, tenant)
    first = _alert(db, tenant, sku)
    _send(NOW)
    # The price dipped, the alert resolved, and it climbed again: a new alert.
    first.status = AlertStatus.resolved
    db.commit()
    again = _alert(db, tenant, sku, created_at=NOW + timedelta(days=2))
    _send(NOW + timedelta(days=2, hours=1))
    assert db.scalar(select(AlertEmailSend.id).where(AlertEmailSend.alert_id == again.id)) is None
    assert len([p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]) == 1

    # A week on, it's news again.
    later = NOW + timedelta(days=8)
    again.created_at = later - timedelta(hours=1)
    db.commit()
    _send(later)
    assert db.scalar(select(AlertEmailSend.id).where(AlertEmailSend.alert_id == again.id)) is not None


def test_a_failed_send_is_retried_on_the_next_run(db, outbox, monkeypatch):
    tenant = _tenant(db)
    alert = _alert(db, tenant, _sku(db))
    user = _user(db, tenant)

    def down(message):
        raise mail.MailError("relay unreachable")

    monkeypatch.setattr(mail, "send", down)
    failed = _send(NOW)
    assert any(user.email in f for f in failed.failed)
    assert db.scalar(select(AlertEmailSend.id).where(AlertEmailSend.user_id == user.id)) is None

    monkeypatch.setattr(mail, "send", lambda message: None)
    _send(NOW)
    assert db.scalar(select(AlertEmailSend.alert_id).where(AlertEmailSend.user_id == user.id)) == alert.id


def test_names_are_escaped(db, outbox):
    tenant = _tenant(db, "<script>x</script> Grill")
    _alert(db, tenant, _sku(db, "<b>Onions</b>"))
    user = _user(db, tenant, name="<i>Dana</i>")
    _send(NOW)
    (path,) = [p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]
    _, _, html_body = _read(path)
    assert "<script>" not in html_body and "<b>Onions" not in html_body and "<i>Dana" not in html_body


# --- Turning them off ----------------------------------------------------------------


def test_the_alerts_link_stops_alerts_and_leaves_the_digest_alone(db):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    query = {"u": str(user.id), "t": digest.unsubscribe_token(user.id, "alerts"), "kind": "alerts"}
    page = client.get("/digest/unsubscribe", params=query)
    assert page.status_code == 200 and "Stop price-increase emails?" in page.text
    assert client.post("/digest/unsubscribe", params=query).status_code == 200
    db.expire_all()
    fresh = db.get(User, user.id)
    assert fresh.alert_emails_enabled is False and fresh.digest_enabled is True

    # A digest link can't be relabelled into an alerts one, or the reverse.
    digest_token = {"u": str(user.id), "t": digest.unsubscribe_token(user.id), "kind": "alerts"}
    assert client.post("/digest/unsubscribe", params=digest_token).status_code == 404
    assert client.post("/digest/unsubscribe", params={**query, "kind": "everything"}).status_code == 404


def test_the_account_page_turns_them_off_and_on(db):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    csrf = {CSRF_HEADER: CSRF_HEADER_VALUE}
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=csrf).status_code == 200
    assert client.get("/auth/me").json()["alert_emails_enabled"] is True
    off = client.patch("/auth/me", json={"alert_emails_enabled": False}, headers=csrf).json()
    assert off["alert_emails_enabled"] is False and off["digest_enabled"] is True
    assert client.patch("/auth/me", json={"alert_emails_enabled": True}, headers=csrf).json()["alert_emails_enabled"]
    actions = db.scalars(
        select(AuditEvent.action).where(AuditEvent.entity_id == user.id, AuditEvent.action.like("user.alert_emails%"))
    ).all()
    assert sorted(actions) == ["user.alert_emails_subscribed", "user.alert_emails_unsubscribed"]


def test_any_failure_after_claiming_hands_the_claims_back(db, outbox, monkeypatch):
    """Not only a refused send: anything between claiming and sending
    (building the email, a lost connection) must leave the alerts unsent,
    not recorded as sent."""
    tenant = _tenant(db)
    alert = _alert(db, tenant, _sku(db))
    user = _user(db, tenant)

    def broken(*args, **kwargs):
        raise RuntimeError("template bug")

    monkeypatch.setattr(alert_emails, "compose", broken)
    run = _send(NOW)
    assert any(user.email in f for f in run.failed)
    assert db.scalar(select(AlertEmailSend.id).where(AlertEmailSend.alert_id == alert.id)) is None

    monkeypatch.undo()
    monkeypatch.setattr(settings, "mail_backend", "outbox")
    monkeypatch.setattr(settings, "outbox_dir", str(outbox))
    assert _send(NOW).sent >= 1
    assert db.scalar(select(AlertEmailSend.alert_id).where(AlertEmailSend.user_id == user.id)) == alert.id
