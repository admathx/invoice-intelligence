"""The weekly digest (app/digest.py): what counts as news, who gets it, one
email per person, safe to run twice, and unsubscribing from it."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email import message_from_bytes, policy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app import digest, mail
from app import email_design as design
from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE, SESSION_COOKIE, hash_password
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    AuditEvent,
    CanonicalSku,
    DigestSend,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceAlert,
    Tenant,
    TenantMembership,
    User,
)
from app.models.enums import AlertStatus, AlertType, BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

pytestmark = pytest.mark.real_auth

NOW = datetime(2026, 9, 28, 13, 0, tzinfo=timezone.utc)  # a Monday, after the 12:00 UTC send time
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mail_backend", "outbox")
    monkeypatch.setattr(settings, "outbox_dir", str(tmp_path / "outbox"))
    return tmp_path / "outbox"


@pytest.fixture()
def db():
    session = SessionLocal(expire_on_commit=False)
    made: dict[str, list] = {"tenants": [], "users": [], "skus": [], "distributors": []}
    session.info["made"] = made
    try:
        yield session
    finally:
        session.rollback()
        tenants, users = made["tenants"], made["users"]
        session.execute(delete(AuditEvent).where(AuditEvent.actor_user_id.in_(users) | AuditEvent.entity_id.in_(users)))
        session.execute(delete(PriceAlert).where(PriceAlert.tenant_id.in_(tenants)))
        session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(tenants)))
        session.execute(delete(Invoice).where(Invoice.tenant_id.in_(tenants)))
        session.execute(delete(User).where(User.id.in_(users)))  # memberships, sessions, digest_sends cascade
        session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(made["skus"])))
        session.execute(delete(Distributor).where(Distributor.id.in_(made["distributors"])))
        session.commit()
        session.close()


def _tenant(db, name="Digest Bistro") -> Tenant:
    t = Tenant(name=f"{name} {uuid.uuid4().hex[:6]}", metro="digest-metro", volume_tier=VolumeTier.under_500k)
    db.add(t)
    db.commit()
    db.info["made"]["tenants"].append(t.id)
    return t


def _user(db, *locations, operator=False, **fields) -> User:
    u = User(
        id=uuid.uuid4(),
        email=f"digest-{uuid.uuid4().hex[:8]}@test.invalid",
        name=fields.pop("name", "Dana"),
        password_hash=hash_password(PASSWORD),
        is_operator=operator,
        **fields,
    )
    db.add(u)
    db.flush()
    for t in locations:
        db.add(TenantMembership(user_id=u.id, tenant_id=t.id))
    db.commit()
    db.info["made"]["users"].append(u.id)
    return u


def _sku(db, name="Cilantro") -> CanonicalSku:
    s = CanonicalSku(name=f"{name} {uuid.uuid4().hex[:6]}", category="test", base_uom=BaseUom.lb)
    db.add(s)
    db.commit()
    db.info["made"]["skus"].append(s.id)
    return s


def _alert(db, tenant, sku, *, created_at, pct="0.24") -> None:
    db.add(
        PriceAlert(
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            alert_type=AlertType.creep,
            baseline_price=Decimal("0.54"),
            current_price=Decimal("0.67"),
            pct_change=Decimal(pct),
            window_start=date(2026, 8, 1),
            window_end=date(2026, 9, 21),
            status=AlertStatus.open,
            created_at=created_at,
        )
    )
    db.commit()


def _invoice(db, tenant, *, status, created_at=NOW - timedelta(days=1), total="100.00", distributor=None) -> Invoice:
    inv = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id if distributor else None,
        invoice_number=f"D-{uuid.uuid4().hex[:5]}",
        total=Decimal(total),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=status,
        created_at=created_at,
    )
    db.add(inv)
    db.commit()
    return inv


def _week(db, tenant) -> digest.LocationWeek:
    return digest.location_week(db, tenant, NOW)


# --- What the week contains ----------------------------------------------------


def test_a_location_week_counts_what_happened_and_what_needs_doing(db):
    tenant = _tenant(db)
    sku = _sku(db)
    _alert(db, tenant, sku, created_at=NOW - timedelta(days=2))
    _alert(db, tenant, _sku(db, "Old news"), created_at=NOW - timedelta(days=12))
    _invoice(db, tenant, status=InvoiceStatus.needs_review)
    _invoice(db, tenant, status=InvoiceStatus.failed, created_at=NOW - timedelta(days=30))  # still unresolved
    _invoice(db, tenant, status=InvoiceStatus.extracted, total="250.50")
    _invoice(db, tenant, status=InvoiceStatus.confirmed, total="49.50")
    _invoice(db, tenant, status=InvoiceStatus.extracted, total="999.00", created_at=NOW - timedelta(days=9))  # last week

    sysco = db.scalar(select(Distributor).where(Distributor.slug == "sysco"))
    other = db.scalar(select(Distributor).where(Distributor.slug == "other"))
    for dist in (sysco, other):
        inv = _invoice(db, tenant, status=InvoiceStatus.extracted, total="0", distributor=dist, created_at=NOW - timedelta(days=20))
        db.add(
            InvoiceLineItem(
                tenant_id=tenant.id, invoice_id=inv.id, line_number=1, raw_description="X", quantity=1, unit_price=1,
                extended_price=1, uom="EA", review_status=ReviewStatus.pending,
            )
        )
    db.commit()

    week = _week(db, tenant)
    assert week.new_increase_count == 1 and week.open_alert_count == 2
    assert week.new_increases[0].sku == sku.name and week.new_increases[0].unit == "lb"
    assert week.held_count == 2
    # Only the line the review queue shows: not the one on an unattributed invoice.
    assert week.pending_lines == 1
    assert (week.received_count, week.received_total) == (2, Decimal("300.00"))
    assert week.has_news


def _offer(verdict="stay", action="Stay with Sysco", **fields):
    from app.analytics.alternatives import Advice, Alternative

    fields = {"website": "https://www.usfoods.com", "annual_saving": Decimal("311.40"), **fields}
    return Alternative(
        uuid.uuid4(), "US Foods", Decimal("0.5361"), yours=False, saving_pct=Decimal("0.1995"),
        advice=Advice(verdict, action, "Not worth a new supplier. Show this price to your Sysco rep.", []), **fields,
    )  # fmt: skip


def test_a_new_increase_says_where_it_costs_less_and_what_to_do(db, monkeypatch):
    """The week's email carries the cheapest alternative of each increase in
    a table: where, for how much, what it saves, and the next step. A lower
    price alone would read as "switch"."""
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db), created_at=NOW - timedelta(days=2))
    monkeypatch.setattr(digest, "weighed_alternatives", lambda db, tenant, alerts, key: {alerts[0].id: [_offer()]})

    week = _week(db, tenant)

    savings = f"https://app.example.com/negotiation?location={tenant.id}"
    assert week.new_increases[0].elsewhere == design.Elsewhere(
        "US Foods", "https://www.usfoods.com", Decimal("0.5361"), Decimal("0.1995"), "about $311", "Stay with Sysco", savings
    )
    _, text, html_body = _parts(digest.compose(_user(db, tenant), [week]))
    assert "Cheaper at US Foods (https://www.usfoods.com): $0.5361/lb, saves about $311 a year." in text
    assert "Next step: Stay with Sysco." in text
    # A table, read across; the distributor links to its site and the next
    # step to the page with the numbers for the rep.
    assert "<th" in html_body and "Cheaper at</th>" in html_body and "Next step</th>" in html_body
    assert ">US Foods</a>" in html_body and "href='https://www.usfoods.com'" in html_body
    assert "$0.5361/lb," in html_body and "20% less" in html_body and "about $311" in html_body
    assert f"href='{savings}'" in html_body and ">Stay with Sysco</a>" in html_body


def test_moving_a_product_links_to_where_it_would_move(db, monkeypatch):
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db), created_at=NOW - timedelta(days=2))
    offer = _offer("move", "Move it to US Foods", annual_saving=None)
    monkeypatch.setattr(digest, "weighed_alternatives", lambda db, tenant, alerts, key: {alerts[0].id: [offer]})

    there = _week(db, tenant).new_increases[0].elsewhere

    assert (there.action, there.action_link, there.saves) == ("Move it to US Foods", "https://www.usfoods.com", None)


def test_the_email_still_goes_when_the_alternatives_cant_be_worked_out(db, monkeypatch):
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db), created_at=NOW - timedelta(days=2))

    def broken(*_):
        raise RuntimeError("no")

    monkeypatch.setattr(digest, "weighed_alternatives", broken)

    week = _week(db, tenant)

    assert week.new_increase_count == 1 and week.new_increases[0].elsewhere is None
    assert digest.compose(_user(db, tenant), [week]) is not None


def test_an_increase_with_nowhere_cheaper_says_nothing_about_it(db):
    tenant = _tenant(db)
    _alert(db, tenant, _sku(db), created_at=NOW - timedelta(days=2))

    week = _week(db, tenant)

    assert week.new_increases[0].elsewhere is None
    _, text, html_body = _parts(digest.compose(_user(db, tenant), [week]))
    assert "Cheaper at" not in text and "Where they cost less" not in html_body


def test_a_quiet_week_sends_nothing(db):
    tenant = _tenant(db)
    user = _user(db, tenant)
    assert not _week(db, tenant).has_news
    assert digest.compose(user, [_week(db, tenant)]) is None


# --- The email -------------------------------------------------------------------


def _parts(message):
    parsed = message_from_bytes(message.as_bytes(), policy=policy.default)
    return parsed, parsed.get_body(("plain",)).get_content(), parsed.get_body(("html",)).get_content()


def test_one_email_covers_every_location_with_news_and_links_to_each(db, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://app.example.com")
    busy, quiet, also_busy = _tenant(db, "Busy"), _tenant(db, "Quiet"), _tenant(db, "Also Busy")
    _invoice(db, busy, status=InvoiceStatus.needs_review)
    _alert(db, also_busy, _sku(db), created_at=NOW - timedelta(days=1))
    user = _user(db, busy, quiet, also_busy)

    message = digest.compose(user, [_week(db, t) for t in (busy, quiet, also_busy)])
    parsed, text, html_body = _parts(message)
    assert parsed["To"] == user.email
    assert parsed["Subject"] == "Your week at 2 locations: 1 price increase, 1 invoice to look at"
    assert busy.name in text and also_busy.name in text and quiet.name not in text
    assert f"https://app.example.com/review" not in text  # nothing pending
    assert f"/insights?location={also_busy.id}" in text
    assert f"/invoices/" in html_body and f"location={busy.id}" in html_body
    # Standard unsubscribe headers, so mail clients offer their own button.
    assert parsed["List-Unsubscribe"].startswith("<https://app.example.com/api/digest/unsubscribe?u=")
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_names_are_escaped_in_the_html(db):
    tenant = _tenant(db, '<script>alert("x")</script> Grill')
    _alert(db, tenant, _sku(db, "<b>Onions</b>"), created_at=NOW - timedelta(days=1))
    user = _user(db, tenant, name="<i>Dana</i>")
    _, _, html_body = _parts(digest.compose(user, [_week(db, tenant)]))
    assert "<script>" not in html_body and "&lt;script&gt;" in html_body
    assert "<b>Onions" not in html_body and "<i>Dana" not in html_body


# --- Who gets it --------------------------------------------------------------------


def test_recipients_are_active_members_who_want_it(db):
    tenant = _tenant(db)
    member = _user(db, tenant)
    _user(db, tenant, is_active=False)
    _user(db, tenant, digest_enabled=False)
    _user(db, operator=True)  # sees every location, belongs to none: would get them all
    mine = {u.id for u, _ in digest.recipients(db) if u.id in db.info["made"]["users"]}
    assert mine == {member.id}


# --- Sending ------------------------------------------------------------------------


def _outbox_files(outbox: Path) -> list[Path]:
    return sorted(outbox.glob("*.eml")) if outbox.exists() else []


def test_sending_is_once_per_person_per_week(db, outbox):
    tenant = _tenant(db)
    _invoice(db, tenant, status=InvoiceStatus.needs_review)
    busy, quiet_tenant = _user(db, tenant), _tenant(db, "Quiet")
    quiet = _user(db, quiet_tenant)
    only = [busy.id, quiet.id]

    first = digest.send_due_digests(db, NOW, only=only)
    assert (first.sent, first.quiet, first.already_sent) == (1, 1, 0)
    assert len(_outbox_files(outbox)) == 1

    again = digest.send_due_digests(db, NOW + timedelta(hours=3), only=only)
    assert (again.sent, again.already_sent) == (0, 2)
    assert len(_outbox_files(outbox)) == 1

    next_week = digest.send_due_digests(db, NOW + timedelta(days=7), only=only)
    assert next_week.sent == 1


def test_sending_works_on_the_schedulers_own_session(db, outbox):
    """The scheduler's session expires everything on each commit; the
    fixture's doesn't, so this runs one the scheduler's way."""
    tenant = _tenant(db)
    _invoice(db, tenant, status=InvoiceStatus.needs_review)
    users = [_user(db, tenant), _user(db, tenant)]
    session = SessionLocal()
    try:
        run = digest.send_due_digests(session, NOW, only=[u.id for u in users])
    finally:
        session.close()
    assert run.sent == 2 and not run.failed


def test_a_failed_send_is_retried_on_the_next_run(db, outbox, monkeypatch):
    tenant = _tenant(db)
    _invoice(db, tenant, status=InvoiceStatus.needs_review)
    user = _user(db, tenant)

    def down(message):
        raise mail.MailError("relay unreachable")

    monkeypatch.setattr(mail, "send", down)
    failed = digest.send_due_digests(db, NOW, only=[user.id])
    assert failed.sent == 0 and len(failed.failed) == 1
    assert db.scalar(select(DigestSend.id).where(DigestSend.user_id == user.id)) is None  # claim released

    monkeypatch.undo()
    monkeypatch.setattr(settings, "outbox_dir", str(outbox))
    assert digest.send_due_digests(db, NOW, only=[user.id]).sent == 1


def test_two_runs_at_once_cannot_both_claim_a_week(db):
    user = _user(db, _tenant(db))
    other = SessionLocal()
    try:
        assert digest._claim(db, user, digest.week_of(NOW), 1) is True
        assert digest._claim(other, other.get(User, user.id), digest.week_of(NOW), 1) is False
    finally:
        other.close()


def test_the_digest_is_due_from_monday_noon_utc_for_the_rest_of_the_week():
    monday = datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert not digest.is_due(monday + timedelta(hours=11, minutes=59))
    assert digest.is_due(monday + timedelta(hours=12))
    assert digest.is_due(monday + timedelta(days=6, hours=23))  # Sunday: still unsent means send
    assert digest.week_of(monday + timedelta(days=6, hours=23)) == monday.date()


# --- Unsubscribing -------------------------------------------------------------------


def _unsubscribe_query(user) -> dict:
    return {"u": str(user.id), "t": digest.unsubscribe_token(user.id)}


def test_opening_the_link_asks_first_and_the_button_unsubscribes(db):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    page = client.get("/digest/unsubscribe", params=_unsubscribe_query(user))
    # A link scanner opening this must not unsubscribe anyone.
    assert page.status_code == 200 and "<form method='post'>" in page.text
    db.expire_all()
    assert db.get(User, user.id).digest_enabled is True

    done = client.post("/digest/unsubscribe", params=_unsubscribe_query(user))
    assert done.status_code == 200 and "Unsubscribed" in done.text
    db.expire_all()
    assert db.get(User, user.id).digest_enabled is False
    assert db.scalar(select(AuditEvent.id).where(AuditEvent.entity_id == user.id, AuditEvent.action == "user.digest_unsubscribed"))


def test_one_click_unsubscribe_works_even_from_a_signed_in_browser(db):
    """RFC 8058: the mail client POSTs "List-Unsubscribe=One-Click", no
    header can be added, and a session cookie may well be present."""
    user = _user(db, _tenant(db))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE, "some-session")
    resp = client.post(
        "/digest/unsubscribe", params=_unsubscribe_query(user), content=b"List-Unsubscribe=One-Click",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code == 200
    db.expire_all()
    assert db.get(User, user.id).digest_enabled is False


def test_a_forged_or_mangled_link_does_nothing(db):
    user, victim = _user(db, _tenant(db)), _user(db, _tenant(db))
    client = TestClient(app)
    forged = client.post("/digest/unsubscribe", params={"u": str(victim.id), "t": digest.unsubscribe_token(user.id)})
    assert forged.status_code == 404
    assert client.get("/digest/unsubscribe", params={"u": "not-a-uuid", "t": "x"}).status_code == 404
    db.expire_all()
    assert db.get(User, victim.id).digest_enabled is True


def test_the_account_page_toggle_turns_it_off_and_on(db):
    user = _user(db, _tenant(db))
    client = TestClient(app)
    csrf = {CSRF_HEADER: CSRF_HEADER_VALUE}
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=csrf).status_code == 200
    assert client.get("/auth/me").json()["digest_enabled"] is True
    assert client.patch("/auth/me", json={"digest_enabled": False}, headers=csrf).json()["digest_enabled"] is False
    assert client.patch("/auth/me", json={"digest_enabled": True}, headers=csrf).json()["digest_enabled"] is True
    actions = db.scalars(select(AuditEvent.action).where(AuditEvent.entity_id == user.id, AuditEvent.action.like("user.digest%"))).all()
    assert sorted(actions) == ["user.digest_subscribed", "user.digest_unsubscribed"]


# --- Delivery and configuration --------------------------------------------------------


def test_smtp_delivery_uses_starttls_and_the_provider_credentials(monkeypatch):
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, username, password):
            calls.append(("login", username))

        def send_message(self, message):
            calls.append(("send", message["To"]))

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "mail_backend", "smtp")
    monkeypatch.setattr(settings, "smtp_host", "smtp.provider.example")
    monkeypatch.setattr(settings, "smtp_username", "apikey")
    monkeypatch.setattr(settings, "smtp_password", "secret")
    mail.send(mail.build_message(to="a@b.example", subject="s", text="t", html="<p>t</p>"))
    assert calls == [("connect", "smtp.provider.example", 587), ("starttls",), ("login", "apikey"), ("send", "a@b.example")]


def test_production_needs_a_signing_key_and_a_real_mail_relay():
    from pydantic import ValidationError

    from app.config import Settings

    good = dict(
        app_env="production",
        session_cookie_secure=True,
        frontend_origins=["https://app.example.com"],
        public_base_url="https://app.example.com",
        secret_key="x" * 40,
        mail_backend="smtp",
    )
    assert Settings(**good)
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(**{**good, "secret_key": ""})
    with pytest.raises(ValidationError, match="MAIL_BACKEND"):
        Settings(**{**good, "mail_backend": "outbox"})
    # Every kind of email needs the relay; with each switched off, none does.
    no_mail = {"digests_enabled": False, "alert_emails_enabled": False, "password_reset_enabled": False}
    for kind in no_mail:
        with pytest.raises(ValidationError, match=kind.upper()):
            Settings(**{**good, **no_mail, kind: True, "mail_backend": "outbox"})
    assert Settings(**{**good, **no_mail, "mail_backend": "outbox"})
