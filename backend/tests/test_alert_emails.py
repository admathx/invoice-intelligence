"""Price-increase emails (app/alert_emails.py): which alerts, to whom, once
each, and turning them off without turning off the digest."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email import message_from_bytes, policy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import alert_emails, digest, mail
from app.analytics import switching
from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import AlertEmailSend, AuditEvent, Distributor, Invoice, PriceAlert, User
from app.models.enums import AlertStatus, AlertType, InvoiceSource, InvoiceStatus

# The digest's fixtures: committed users, locations and products, cleaned up after.
from test_digest import PASSWORD, _offer, _outbox_files, _sku, _tenant, _user, db, outbox  # noqa: F401

pytestmark = pytest.mark.real_auth

# Years ahead: only alerts these tests make fall inside the lookback, never
# the development database's own, which would otherwise be emailed and recorded.
NOW = datetime(2031, 3, 3, 15, 0, tzinfo=timezone.utc)


def _alert(
    db, tenant, sku, *, pct="0.24", created_at=NOW - timedelta(hours=1), status=AlertStatus.open, distributor=None,
    window_end=NOW.date() - timedelta(days=4),
) -> PriceAlert:  # fmt: skip
    alert = PriceAlert(
        tenant_id=tenant.id,
        canonical_sku_id=sku.id,
        distributor_id=distributor.id if distributor else None,
        alert_type=AlertType.creep,
        baseline_price=Decimal("0.54"),
        current_price=Decimal("0.67"),
        pct_change=Decimal(pct),
        window_start=window_end - timedelta(days=60),
        window_end=window_end,
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


def _distributor(db, slug):
    return db.scalar(select(Distributor).where(Distributor.slug == slug))


def test_an_increase_says_whose_price_went_up(db, outbox):
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db, "Cups Hot"), distributor=_distributor(db, "sysco"))
    user = _user(db, tenant)
    _send(NOW)
    (path,) = [p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]
    _, text, html_body = _read(path)
    assert "Cups Hot" in text and "from Sysco" in text and "from Sysco" in html_body


def test_an_increase_says_where_it_costs_less_and_what_to_do(db, outbox, monkeypatch):
    """The email that says a price went up is the one read before the next
    order, so it is where "and it's cheaper at..." is most use. A table:
    where, for how much, what it saves in a year, and the next step."""
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    here, there = _tenant(db, "Here"), _tenant(db, "There")
    flagged = _alert(db, here, _sku(db, "Cilantro"), distributor=_distributor(db, "sysco"))
    _alert(db, there, _sku(db, "Limes"), distributor=_distributor(db, "sysco"))
    user, other = _user(db, here), _user(db, there)
    asked = []

    def offers(db, tenant, alerts, key):
        asked.append((tenant.id, db.info.get("tenant_id")))
        return {flagged.id: [_offer()]} if tenant.id == here.id else {}

    monkeypatch.setattr(switching, "weighed_alternatives", offers)

    _send(NOW)

    mine, theirs = ([p for p in _outbox_files(outbox) if u.email.replace("@", "_at_") in p.name] for u in (user, other))
    _, text, html_body = _read(mine[0])
    assert "Cheaper at US Foods (https://www.usfoods.com): $0.5361/lb, saves about $311 a year." in text
    assert "Next step: Stay with Sysco." in text
    assert "Cheaper at</th>" in html_body and "href='https://www.usfoods.com'" in html_body and "about $311" in html_body
    assert f"href='https://app.example.com/negotiation?location={here.id}'" in html_body
    # Each location's own invoices were read with that location bound, and
    # the other location's people still got theirs, with nothing about it.
    assert sorted(asked) == sorted([(here.id, here.id), (there.id, there.id)])
    assert len(theirs) == 1 and "Cheaper at" not in _read(theirs[0])[1]


def test_a_check_with_nothing_to_send_doesnt_work_out_alternatives(db, outbox, monkeypatch):
    """The scheduler checks every few minutes, and an alert stays in the
    lookback for two days after everyone has had it. Working out where its
    product costs less is for an email about to go, not for every check."""
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db, "Cilantro"), distributor=_distributor(db, "sysco"))
    _user(db, tenant)
    asked = []
    monkeypatch.setattr(switching, "weighed_alternatives", lambda db, tenant, alerts, key: asked.append(tenant.id) or {})

    _send(NOW)
    _send(NOW + timedelta(minutes=5))

    assert asked == [tenant.id], "once, for the email that went; not again for the check that sent nothing"


def test_the_increase_is_still_sent_when_the_alternatives_cant_be_worked_out(db, outbox, monkeypatch):
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db, "Cilantro"), distributor=_distributor(db, "sysco"))
    user = _user(db, tenant)

    def broken(*_):
        raise RuntimeError("no")

    monkeypatch.setattr(switching, "weighed_alternatives", broken)

    _send(NOW)

    (path,) = [p for p in _outbox_files(outbox) if user.email.replace("@", "_at_") in p.name]
    assert "Cilantro" in _read(path)[1]


def test_the_same_product_from_another_distributor_is_its_own_news(db, outbox):
    """The week's quiet is per distributor: US Foods raising a price is news
    even if Sysco's increase on the same product was emailed yesterday."""
    tenant, sku = _tenant(db), _sku(db)
    user = _user(db, tenant)
    first = _alert(db, tenant, sku, distributor=_distributor(db, "sysco"))
    _send(NOW)
    first.status = AlertStatus.resolved
    db.commit()
    other = _alert(db, tenant, sku, created_at=NOW + timedelta(days=1), distributor=_distributor(db, "us_foods"))
    _send(NOW + timedelta(days=1, hours=1))
    assert db.scalar(select(AlertEmailSend.id).where(AlertEmailSend.alert_id == other.id)) is not None


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


# --- A box of old invoices (the fourth test set, replayed in random order) -----


def _invoice(db, tenant, *, created_at, status=InvoiceStatus.extracted) -> Invoice:
    invoice = Invoice(
        tenant_id=tenant.id, source=InvoiceSource.upload, status=status, original_file_uri="file:///dev/null",
        created_at=created_at,
    )  # fmt: skip
    db.add(invoice)
    db.commit()
    return invoice


def _emailed(db, user) -> set:
    """The alerts this person has been emailed about."""
    return set(db.scalars(select(AlertEmailSend.alert_id).where(AlertEmailSend.user_id == user.id)))


def test_alerts_wait_until_the_locations_invoices_have_stopped_arriving(db, outbox):
    """Added in whatever order they come out of the box, invoices build a
    price history in pieces, and a piece can look like an increase the rest
    takes away again. Nothing is emailed mid-box."""
    tenant = _tenant(db)
    user = _user(db, tenant)
    alert = _alert(db, tenant, _sku(db, "Limes"))
    arriving = _invoice(db, tenant, created_at=NOW - timedelta(minutes=3))

    _send(NOW)
    assert _emailed(db, user) == set()

    # One is still being read, long after it was added.
    arriving.created_at = NOW - timedelta(hours=2)
    arriving.status = InvoiceStatus.extracting
    db.commit()
    _send(NOW + timedelta(minutes=5))
    assert _emailed(db, user) == set()

    # Settled: what is still open is sent.
    arriving.status = InvoiceStatus.extracted
    db.commit()
    _send(NOW + timedelta(minutes=20))
    assert _emailed(db, user) == {alert.id}


def test_another_locations_invoices_dont_hold_this_ones_alerts(db, outbox):
    tenant, busy = _tenant(db), _tenant(db, "Busy")
    user = _user(db, tenant)
    alert = _alert(db, tenant, _sku(db, "Limes"))
    _invoice(db, busy, created_at=NOW - timedelta(minutes=1))
    _send(NOW)
    assert _emailed(db, user) == {alert.id}


def test_an_alert_that_closed_while_the_box_was_being_added_is_never_sent(db, outbox):
    tenant = _tenant(db)
    user = _user(db, tenant)
    alert = _alert(db, tenant, _sku(db, "Limes"))
    _invoice(db, tenant, created_at=NOW - timedelta(minutes=3))
    _send(NOW)
    alert.status = AlertStatus.resolved  # the rest of the box took it away
    db.commit()
    _send(NOW + timedelta(minutes=30))
    assert _emailed(db, user) == set()


def test_an_increase_in_old_prices_is_history_not_news(db, outbox):
    """Last year's invoices, added today: the alert is real and shown on the
    Price alerts page, but it isn't something to email about now."""
    tenant = _tenant(db)
    user = _user(db, tenant)
    _alert(db, tenant, _sku(db, "Old increase"), window_end=NOW.date() - timedelta(days=200))
    recent = _alert(db, tenant, _sku(db, "Recent increase"), window_end=NOW.date() - timedelta(days=10))
    # A month's invoices added early the next month: something bought on the
    # 1st is five weeks old by then, and still news.
    month_end = _alert(db, tenant, _sku(db, "Month-end batch"), window_end=NOW.date() - timedelta(days=35))

    _send(NOW)
    assert _emailed(db, user) == {recent.id, month_end.id}

    # Nor is the old one "new this week" in the Monday summary.
    week = digest.location_week(db, tenant, NOW)
    assert week.new_increase_count == 2 and week.open_alert_count == 3


def test_a_price_under_a_dollar_keeps_the_places_that_show_its_increase():
    """To the cent, a napkin going from $0.0125 to $0.0131 read "$0.01 -> $0.01"."""
    from app.email_design import price_per, unit_price

    assert unit_price(Decimal("18.0618")) == "$18.06" and unit_price(Decimal("1234.5")) == "$1,234.50"
    assert unit_price(Decimal("0.6656")) == "$0.6656" and unit_price(Decimal("0.500000")) == "$0.50"
    assert unit_price(Decimal("0.012477")) == "$0.0125" and unit_price(Decimal("0.013101")) == "$0.0131"
    assert unit_price(Decimal("0.004167")) == "$0.004167" and unit_price(Decimal("0.000040")) == "$0.00004"
    assert price_per(Decimal("0.0659"), "each") == "$0.0659/each" and price_per(Decimal("3.34"), "") == "$3.34"
