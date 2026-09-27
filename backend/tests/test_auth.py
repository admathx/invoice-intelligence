"""Sign-in, sessions, who may open which location, and the audit trail.

These run with real sign-in (the `real_auth` marker turns off conftest's
injected operator): a password posted to /auth/login, the cookie it sets, and
that cookie carried on every later request, exactly as a browser does.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, or_, select, update

from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE, SESSION_COOKIE, hash_password
from app.config import settings
from app.db import SessionLocal
from app.main import app
from app.models import (
    AuditEvent,
    CanonicalSku,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceObservation,
    SkuAlias,
    Tenant,
    TenantMembership,
    User,
    UserSession,
)
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery staple"
CSRF = {CSRF_HEADER: CSRF_HEADER_VALUE}


@pytest.fixture()
def db():
    """Committing session (the API opens its own). Everything created is
    removed in FK order, audit events included. No expiry on commit: the
    fixtures span several tenants, so reloading an expired Invoice would trip
    the tenant guard with no tenant bound."""
    session = SessionLocal(expire_on_commit=False)
    created: dict[str, list] = {"tenants": [], "users": [], "distributors": [], "skus": []}
    session.info["_created"] = created
    try:
        yield session
    finally:
        session.rollback()
        tenants, users = created["tenants"], created["users"]
        session.execute(
            delete(AuditEvent).where(
                or_(AuditEvent.tenant_id.in_(tenants), AuditEvent.actor_user_id.in_(users), AuditEvent.entity_id.in_(users))
            )
        )
        if tenants:
            lines = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id.in_(tenants))
            session.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(lines)))
            session.execute(delete(SkuAlias).where(SkuAlias.tenant_id.in_(tenants)))
            session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(tenants)))
            session.execute(delete(Invoice).where(Invoice.tenant_id.in_(tenants)))
        session.execute(delete(User).where(User.id.in_(users)))  # memberships and sessions cascade
        session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(created["skus"])))
        session.execute(delete(Distributor).where(Distributor.id.in_(created["distributors"])))
        session.commit()
        session.close()


def _tenant(db, name: str) -> Tenant:
    tenant = Tenant(name=f"{name} {uuid.uuid4().hex[:8]}", metro="auth-test-metro", volume_tier=VolumeTier.under_500k)
    db.add(tenant)
    db.commit()
    db.info["_created"]["tenants"].append(tenant.id)
    return tenant


def _user(db, *, operator: bool = False, locations: tuple[Tenant, ...] = (), email: str | None = None) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email or f"auth-test-{uuid.uuid4().hex[:8]}@test.invalid",
        name="Auth Test",
        password_hash=hash_password(PASSWORD),
        is_operator=operator,
    )
    db.add(user)
    db.flush()
    for tenant in locations:
        db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id))
    db.commit()
    db.info["_created"]["users"].append(user.id)
    return user


def _signed_in(user: User) -> TestClient:
    client = TestClient(app)
    resp = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    return client


def _distributor(db) -> Distributor:
    distributor = Distributor(name="Auth Distributor", slug=f"auth-dist-{uuid.uuid4().hex[:8]}")
    db.add(distributor)
    db.commit()
    db.info["_created"]["distributors"].append(distributor.id)
    return distributor


def _needs_review_invoice(db, tenant: Tenant, distributor: Distributor | None = None) -> Invoice:
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id if distributor else None,
        invoice_number="AUTH-0001",
        invoice_date=date(2026, 5, 1),
        subtotal=Decimal("20.00"),
        tax=Decimal("0.00"),
        total=Decimal("20.00"),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.needs_review,
    )
    db.add(invoice)
    db.add(
        InvoiceLineItem(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_id=invoice.id,
            line_number=1,
            raw_description="AUTH TEST LINE",
            raw_sku="AT-1",
            raw_pack_size="1 EA",
            quantity=Decimal("2"),
            unit_price=Decimal("12.00"),
            extended_price=Decimal("20.00"),
            uom="EA",
            review_status=ReviewStatus.pending,
        )
    )
    db.commit()
    return invoice


def _unscoped(db, statement):
    """These tests span tenants on one session, so reads opt out of the tenant
    guard explicitly, the same way the worker does."""
    return db.scalar(statement.execution_options(tenant_scope_bypass=True))


# --- Signing in -------------------------------------------------------------


def test_everything_but_health_and_login_requires_signing_in(db):
    tenant = _tenant(db, "Anon")
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    for path in ("/auth/me", f"/invoices?tenant_id={tenant.id}", "/distributors", "/skus?q=milk", "/accounts", "/tenants"):
        assert client.get(path).status_code == 401, path


def test_sign_in_sets_an_http_only_session_cookie_and_me_lists_only_my_locations(db):
    mine, _theirs = _tenant(db, "Mine"), _tenant(db, "Theirs")
    user = _user(db, locations=(mine,))
    client = TestClient(app)

    resp = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    set_cookie = resp.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie

    me = client.get("/auth/me").json()
    assert me["email"] == user.email and me["is_operator"] is False
    assert [loc["id"] for loc in me["locations"]] == [str(mine.id)]

    # Only a hash of the token is stored.
    token = client.cookies[SESSION_COOKIE]
    stored = db.scalar(select(UserSession.token_hash).where(UserSession.user_id == user.id))
    assert stored and stored != token


def test_email_is_matched_case_insensitively(db):
    user = _user(db, email=f"Mixed.Case-{uuid.uuid4().hex[:6]}@test.invalid".lower())
    resp = TestClient(app).post(
        "/auth/login", json={"email": f"  {user.email.upper()} ", "password": PASSWORD}, headers=CSRF
    )
    assert resp.status_code == 200, resp.text


def test_wrong_password_and_unknown_email_fail_identically_and_are_logged(db):
    user = _user(db)
    client = TestClient(app)
    wrong = client.post("/auth/login", json={"email": user.email, "password": "nope"}, headers=CSRF)
    unknown_email = f"nobody-{uuid.uuid4().hex[:8]}@test.invalid"
    unknown = client.post("/auth/login", json={"email": unknown_email, "password": "nope"}, headers=CSRF)
    try:
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()
        assert SESSION_COOKIE not in wrong.cookies

        failures = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "auth.login_failed",
                AuditEvent.details["email"].astext.in_([user.email, unknown_email]),
            )
        ).all()
        assert {e.details["email"] for e in failures} == {user.email, unknown_email}
    finally:
        db.execute(delete(AuditEvent).where(AuditEvent.details["email"].astext == unknown_email))
        db.commit()


def test_repeated_failures_lock_the_address_even_for_the_right_password(db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    user = _user(db)
    client = TestClient(app)
    for _ in range(3):
        assert client.post("/auth/login", json={"email": user.email, "password": "nope"}, headers=CSRF).status_code == 401
    resp = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF)
    assert resp.status_code == 429


def test_a_successful_sign_in_resets_the_count(db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 2)
    user = _user(db)
    client = TestClient(app)
    wrong = {"email": user.email, "password": "nope"}
    right = {"email": user.email, "password": PASSWORD}
    assert client.post("/auth/login", json=wrong, headers=CSRF).status_code == 401
    assert client.post("/auth/login", json=right, headers=CSRF).status_code == 200
    assert client.post("/auth/login", json=wrong, headers=CSRF).status_code == 401
    # One failure since the success, not two in the window.
    assert client.post("/auth/login", json=right, headers=CSRF).status_code == 200


def test_failures_outside_the_window_dont_count(db, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 2)
    user = _user(db)
    client = TestClient(app)
    for _ in range(2):
        client.post("/auth/login", json={"email": user.email, "password": "nope"}, headers=CSRF)
    db.execute(
        update(AuditEvent)
        .where(AuditEvent.action == "auth.login_failed", AuditEvent.details["email"].astext == user.email)
        .values(occurred_at=datetime.now(timezone.utc) - timedelta(minutes=settings.login_failure_window_minutes + 1))
    )
    db.commit()
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF).status_code == 200


def test_sign_in_requires_the_csrf_header(db):
    user = _user(db)
    resp = TestClient(app).post("/auth/login", json={"email": user.email, "password": PASSWORD})
    assert resp.status_code == 403


def test_a_deactivated_user_cannot_sign_in_and_is_signed_out(db):
    user = _user(db)
    client = _signed_in(user)
    db.execute(update(User).where(User.id == user.id).values(is_active=False))
    db.commit()
    assert client.get("/auth/me").status_code == 401
    resp = TestClient(app).post("/auth/login", json={"email": user.email, "password": PASSWORD}, headers=CSRF)
    assert resp.status_code == 401


def test_an_expired_session_is_refused(db):
    user = _user(db)
    client = _signed_in(user)
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id)
        .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    db.commit()
    assert client.get("/auth/me").status_code == 401


def test_sign_out_revokes_the_session_server_side(db):
    user = _user(db)
    client = _signed_in(user)
    token = client.cookies[SESSION_COOKIE]
    assert client.post("/auth/logout", headers=CSRF).status_code == 204

    # A copy of the old cookie (say, from before the browser dropped it) is dead.
    replay = TestClient(app)
    replay.cookies.set(SESSION_COOKIE, token)
    assert replay.get("/auth/me").status_code == 401


# --- CSRF ---------------------------------------------------------------------


def test_a_write_riding_the_session_cookie_needs_the_csrf_header(db):
    tenant = _tenant(db, "Csrf")
    user = _user(db, locations=(tenant,))
    invoice = _needs_review_invoice(db, tenant)
    client = _signed_in(user)

    forged = client.patch(f"/invoices/{invoice.id}", params={"tenant_id": str(tenant.id)}, json={"total": "1.00"})
    assert forged.status_code == 403
    assert _unscoped(db, select(Invoice.total).where(Invoice.id == invoice.id)) == Decimal("20.00")

    genuine = client.patch(
        f"/invoices/{invoice.id}", params={"tenant_id": str(tenant.id)}, json={"total": "24.00"}, headers=CSRF
    )
    assert genuine.status_code == 200, genuine.text


# --- Who may open what --------------------------------------------------------


def test_a_member_sees_their_location_and_not_anyone_elses(db):
    mine, theirs = _tenant(db, "Mine"), _tenant(db, "Theirs")
    their_invoice = _needs_review_invoice(db, theirs)
    client = _signed_in(_user(db, locations=(mine,)))

    assert client.get("/invoices", params={"tenant_id": str(mine.id)}).status_code == 200
    # 404, not 403: the response doesn't confirm the location exists.
    for path in ("/invoices", "/insights", "/negotiation", "/review/queue", "/activity"):
        assert client.get(path, params={"tenant_id": str(theirs.id)}).status_code == 404, path
    assert client.get(f"/invoices/{their_invoice.id}", params={"tenant_id": str(theirs.id)}).status_code == 404
    # Naming your own location doesn't reach another's invoice either (tenant scope).
    assert client.get(f"/invoices/{their_invoice.id}", params={"tenant_id": str(mine.id)}).status_code == 404
    resp = client.patch(
        f"/invoices/{their_invoice.id}", params={"tenant_id": str(theirs.id)}, json={"total": "1.00"}, headers=CSRF
    )
    assert resp.status_code == 404


def test_businesses_and_the_tenant_list_are_operator_only(db):
    tenant = _tenant(db, "Member")
    member = _signed_in(_user(db, locations=(tenant,)))
    assert member.get("/accounts").status_code == 403
    assert member.get("/tenants").status_code == 403
    assert member.post("/accounts", json={"name": "Nope"}, headers=CSRF).status_code == 403

    operator = _signed_in(_user(db, operator=True))
    assert operator.get("/accounts").status_code == 200
    assert operator.get("/invoices", params={"tenant_id": str(tenant.id)}).status_code == 200
    me = operator.get("/auth/me").json()
    assert str(tenant.id) in {loc["id"] for loc in me["locations"]}


def test_page_images_are_authorized_like_the_invoice(db, isolated_storage):
    from app.storage import render_key

    mine, theirs = _tenant(db, "Mine"), _tenant(db, "Theirs")
    invoice = _needs_review_invoice(db, theirs)
    isolated_storage.put(render_key(invoice.id, "page_001.png"), b"\x89PNG\r\n\x1a\nfake")

    owner = _signed_in(_user(db, locations=(theirs,)))
    detail = owner.get(f"/invoices/{invoice.id}", params={"tenant_id": str(theirs.id)}).json()
    assert detail["page_image_urls"] == [f"/invoices/{invoice.id}/pages/page_001.png?tenant_id={theirs.id}"]
    resp = owner.get(detail["page_image_urls"][0])
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "no-store"
    assert owner.get(f"/invoices/{invoice.id}/pages/..%2F..%2Fsecret.png", params={"tenant_id": str(theirs.id)}).status_code == 404

    outsider = _signed_in(_user(db, locations=(mine,)))
    assert outsider.get(detail["page_image_urls"][0]).status_code == 404
    assert outsider.get(f"/invoices/{invoice.id}/pages/page_001.png", params={"tenant_id": str(mine.id)}).status_code == 404
    assert TestClient(app).get(detail["page_image_urls"][0]).status_code == 401


# --- The audit trail ----------------------------------------------------------


def test_an_edit_is_recorded_with_who_and_what_it_said_before(db):
    tenant = _tenant(db, "Audit")
    user = _user(db, locations=(tenant,))
    invoice = _needs_review_invoice(db, tenant, _distributor(db))
    line_id = _unscoped(db, select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id))
    client = _signed_in(user)

    resp = client.patch(
        f"/invoices/{invoice.id}",
        params={"tenant_id": str(tenant.id)},
        json={"total": "24.00", "subtotal": "24.00", "line_items": [{"id": str(line_id), "extended_price": "24.00"}]},
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text

    history = client.get(f"/invoices/{invoice.id}/history", params={"tenant_id": str(tenant.id)}).json()
    edit = next(e for e in history if e["action"] == "invoice.edited")
    assert edit["actor_email"] == user.email
    assert edit["details"]["changes"]["total"] == {"from": "20.00", "to": "24.00"}
    assert edit["details"]["line_changes"]["1"]["extended_price"] == {"from": "20.00", "to": "24.00"}

    # Confirming is recorded too, and appears in the location's activity feed.
    assert client.post(f"/invoices/{invoice.id}/confirm", params={"tenant_id": str(tenant.id)}, headers=CSRF).status_code == 200
    activity = client.get("/activity", params={"tenant_id": str(tenant.id)}).json()
    assert [e["action"] for e in activity][:2] == ["invoice.confirmed", "invoice.edited"]


def test_a_removed_line_stays_in_its_invoices_history(db):
    tenant = _tenant(db, "Audit")
    user = _user(db, locations=(tenant,))
    invoice = _needs_review_invoice(db, tenant)
    line_id = _unscoped(db, select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id))
    client = _signed_in(user)

    resp = client.delete(f"/invoices/{invoice.id}/line-items/{line_id}", params={"tenant_id": str(tenant.id)}, headers=CSRF)
    assert resp.status_code == 200, resp.text
    history = client.get(f"/invoices/{invoice.id}/history", params={"tenant_id": str(tenant.id)}).json()
    removed = next(e for e in history if e["action"] == "invoice_line.removed")
    assert removed["entity_id"] == str(line_id)
    assert removed["details"]["values"]["raw_description"] == "AUTH TEST LINE"


def test_a_failed_edit_records_nothing(db):
    tenant = _tenant(db, "Audit")
    user = _user(db, locations=(tenant,))
    invoice = _needs_review_invoice(db, tenant)
    client = _signed_in(user)
    resp = client.patch(
        f"/invoices/{invoice.id}",
        params={"tenant_id": str(tenant.id)},
        json={"line_items": [{"id": str(uuid.uuid4()), "quantity": "3"}]},
        headers=CSRF,
    )
    assert resp.status_code == 422
    assert client.get(f"/invoices/{invoice.id}/history", params={"tenant_id": str(tenant.id)}).json() == []


def test_a_review_correction_names_who_made_it(db):
    tenant = _tenant(db, "Review")
    user = _user(db, locations=(tenant,))
    sku = CanonicalSku(name=f"Auth SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.each)
    db.add(sku)
    db.commit()
    db.info["_created"]["skus"].append(sku.id)
    invoice = _needs_review_invoice(db, tenant, _distributor(db))
    line_id = _unscoped(db, select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id == invoice.id))

    client = _signed_in(user)
    resp = client.post(
        f"/review/{line_id}/correct",
        params={"tenant_id": str(tenant.id)},
        json={"canonical_sku_id": str(sku.id)},
        headers=CSRF,
    )
    assert resp.status_code == 200, resp.text
    assert db.scalar(select(SkuAlias.confirmed_by_user_id).where(SkuAlias.source_invoice_line_item_id == line_id)) == user.id
    event = db.scalar(select(AuditEvent).where(AuditEvent.entity_id == line_id))
    assert event.action == "invoice_line.match_corrected" and event.actor_user_id == user.id
    assert event.details["canonical_sku"] == {"from": None, "to": str(sku.id)}
    assert event.details["invoice_id"] == str(invoice.id)


# --- Managing users (the operators' Users screen) ------------------------------


def _created(db, body: dict) -> None:
    """Register a user the API made, for the fixture to clean up."""
    db.info["_created"]["users"].append(uuid.UUID(body["user"]["id"]))


def test_an_operator_creates_a_login_that_works_with_the_generated_password(db):
    tenant = _tenant(db, "Users")
    operator = _signed_in(_user(db, operator=True))
    email = f"new-{uuid.uuid4().hex[:8]}@test.invalid"

    resp = operator.post(
        "/users",
        json={"email": f" {email.upper()} ", "name": "New Person", "location_ids": [str(tenant.id)]},
        headers=CSRF,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    _created(db, body)
    assert resp.headers["cache-control"] == "no-store"
    assert body["user"]["email"] == email
    assert [loc["id"] for loc in body["user"]["locations"]] == [str(tenant.id)]
    password = body["generated_password"]
    assert len(password) >= 12

    newcomer = TestClient(app)
    login = newcomer.post("/auth/login", json={"email": email, "password": password}, headers=CSRF)
    assert login.status_code == 200
    assert [loc["id"] for loc in login.json()["locations"]] == [str(tenant.id)]

    listed = {u["email"]: u for u in operator.get("/users").json()}
    assert listed[email]["last_sign_in"] is not None


def test_creating_a_login_refuses_bad_input(db):
    existing = _user(db)
    operator = _signed_in(_user(db, operator=True))
    duplicate = operator.post("/users", json={"email": existing.email, "name": "Dup"}, headers=CSRF)
    assert duplicate.status_code == 409
    short = operator.post(
        "/users", json={"email": "short-pw@test.invalid", "name": "Short", "password": "tooshort"}, headers=CSRF
    )
    assert short.status_code == 422 and "12 characters" in short.json()["detail"]
    nowhere = operator.post(
        "/users",
        json={"email": "nowhere@test.invalid", "name": "Nowhere", "location_ids": [str(uuid.uuid4())]},
        headers=CSRF,
    )
    assert nowhere.status_code == 422
    assert db.scalar(select(User.id).where(User.email.in_(["short-pw@test.invalid", "nowhere@test.invalid"]))) is None


def test_only_operators_manage_users(db):
    tenant = _tenant(db, "Users")
    member = _signed_in(_user(db, locations=(tenant,)))
    assert member.get("/users").status_code == 403
    assert member.post("/users", json={"email": "x@test.invalid", "name": "X"}, headers=CSRF).status_code == 403


def test_granting_and_revoking_a_location_takes_effect_and_is_recorded(db):
    first, second = _tenant(db, "First"), _tenant(db, "Second")
    operator_user = _user(db, operator=True)
    operator = _signed_in(operator_user)
    member_user = _user(db, locations=(first,))
    member = _signed_in(member_user)
    assert member.get("/invoices", params={"tenant_id": str(second.id)}).status_code == 404

    granted = operator.put(f"/users/{member_user.id}/locations/{second.id}", headers=CSRF)
    assert granted.status_code == 200
    assert member.get("/invoices", params={"tenant_id": str(second.id)}).status_code == 200

    revoked = operator.delete(f"/users/{member_user.id}/locations/{first.id}", headers=CSRF)
    assert [loc["id"] for loc in revoked.json()["locations"]] == [str(second.id)]
    assert member.get("/invoices", params={"tenant_id": str(first.id)}).status_code == 404

    events = db.scalars(
        select(AuditEvent).where(AuditEvent.entity_id == member_user.id, AuditEvent.action.like("user.access_%"))
    ).all()
    assert {(e.action, e.tenant_id, e.actor_user_id) for e in events} == {
        ("user.access_granted", second.id, operator_user.id),
        ("user.access_revoked", first.id, operator_user.id),
    }


def test_deactivating_signs_the_user_out_at_once(db):
    tenant = _tenant(db, "Users")
    operator = _signed_in(_user(db, operator=True))
    member_user = _user(db, locations=(tenant,))
    member = _signed_in(member_user)

    resp = operator.patch(f"/users/{member_user.id}", json={"is_active": False}, headers=CSRF)
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    assert member.get("/auth/me").status_code == 401

    # Reactivating doesn't resurrect the old session; they sign in again.
    operator.patch(f"/users/{member_user.id}", json={"is_active": True}, headers=CSRF)
    assert member.get("/auth/me").status_code == 401
    assert _signed_in(member_user).get("/auth/me").status_code == 200


def test_an_operator_cannot_demote_or_deactivate_themselves(db):
    operator_user = _user(db, operator=True)
    operator = _signed_in(operator_user)
    for change in ({"is_operator": False}, {"is_active": False}):
        resp = operator.patch(f"/users/{operator_user.id}", json=change, headers=CSRF)
        assert resp.status_code == 422, change
    assert operator.get("/users").status_code == 200


def test_a_password_reset_replaces_the_password_and_ends_old_sessions(db):
    operator = _signed_in(_user(db, operator=True))
    member_user = _user(db)
    member = _signed_in(member_user)

    resp = operator.post(f"/users/{member_user.id}/password", json={}, headers=CSRF)
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    new_password = resp.json()["generated_password"]
    assert member.get("/auth/me").status_code == 401

    fresh = TestClient(app)
    assert fresh.post("/auth/login", json={"email": member_user.email, "password": PASSWORD}, headers=CSRF).status_code == 401
    assert fresh.post("/auth/login", json={"email": member_user.email, "password": new_password}, headers=CSRF).status_code == 200
    assert db.scalar(
        select(AuditEvent.id).where(AuditEvent.entity_id == member_user.id, AuditEvent.action == "user.password_reset")
    )


def test_old_dead_sessions_are_pruned_on_sign_in_and_recent_ones_kept(db):
    from app.auth import prune_sessions

    user = _user(db)
    now = datetime.now(timezone.utc)
    long_ago = now - timedelta(days=settings.session_retention_days + 1)
    recently = now - timedelta(days=1)
    db.add_all(
        [
            UserSession(user_id=user.id, token_hash=uuid.uuid4().hex, expires_at=long_ago),
            UserSession(user_id=user.id, token_hash=uuid.uuid4().hex, expires_at=now + timedelta(days=1), revoked_at=long_ago),
            UserSession(user_id=user.id, token_hash=uuid.uuid4().hex, expires_at=recently),
            UserSession(user_id=user.id, token_hash=uuid.uuid4().hex, expires_at=now + timedelta(days=1)),
        ]
    )
    db.commit()
    prune_sessions(db)
    db.commit()
    remaining = db.scalars(select(UserSession.expires_at).where(UserSession.user_id == user.id)).all()
    assert len(remaining) == 2


def test_production_refuses_insecure_cookies_and_http_origins():
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError, match="SESSION_COOKIE_SECURE"):
        Settings(app_env="production", session_cookie_secure=False, frontend_origins=["https://app.example.com"])
    with pytest.raises(ValidationError, match="https"):
        Settings(app_env="production", session_cookie_secure=True, frontend_origins=["http://app.example.com"])
    assert Settings(app_env="production", session_cookie_secure=True, frontend_origins=["https://a.example.com"])


# --- The operators' audit log ------------------------------------------------


def test_the_audit_log_is_operator_only_and_covers_events_with_no_location(db):
    tenant = _tenant(db, "Audit Log")
    member = _signed_in(_user(db, locations=(tenant,)))
    assert member.get("/audit").status_code == 403

    operator_user = _user(db, operator=True)
    operator = _signed_in(operator_user)
    created = operator.post("/users", json={"email": f"log-{uuid.uuid4().hex[:8]}@test.invalid", "name": "Logged"}, headers=CSRF)
    _created(db, created.json())

    page = operator.get("/audit", params={"actor_id": str(operator_user.id)}).json()
    actions = [e["action"] for e in page["events"]]
    # Newest first, and in the order they happened even within one transaction.
    assert actions[:1] == ["user.created"] and "auth.login" in actions
    created_event = page["events"][0]
    assert created_event["tenant_id"] is None and created_event["actor_email"] == operator_user.email


def test_the_audit_log_filters_by_action_family_location_and_invoice(db):
    first, second = _tenant(db, "First"), _tenant(db, "Second")
    operator_user = _user(db, operator=True)
    operator = _signed_in(operator_user)
    member_user = _user(db)
    invoice = _needs_review_invoice(db, first)
    operator.patch(f"/invoices/{invoice.id}", params={"tenant_id": str(first.id)}, json={"total": "21.00"}, headers=CSRF)
    operator.put(f"/users/{member_user.id}/locations/{second.id}", headers=CSRF)

    def actions(**params):
        return {e["action"] for e in operator.get("/audit", params={"actor_id": str(operator_user.id), **params}).json()["events"]}

    assert actions(action="user.") == {"user.access_granted"}
    assert actions(action="invoice") == {"invoice.edited"}  # "invoice" also covers invoice_line.*, none here
    assert actions(action="invoice_") == set()  # "_" is literal, not a wildcard
    assert actions(tenant_id=str(second.id)) == {"user.access_granted"}
    assert actions(entity_id=str(invoice.id)) == {"invoice.edited"}
    assert operator.get("/audit", params={"action": "user%"}).status_code == 422


def test_audit_log_pages_never_skip_or_repeat(db):
    operator_user = _user(db, operator=True)
    operator = _signed_in(operator_user)
    for i in range(7):
        created = operator.post("/users", json={"email": f"page-{i}-{uuid.uuid4().hex[:6]}@test.invalid", "name": f"P{i}"}, headers=CSRF)
        _created(db, created.json())

    seen, cursor = [], None
    while True:
        params = {"actor_id": str(operator_user.id), "action": "user.", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = operator.get("/audit", params=params).json()
        seen += [e["id"] for e in page["events"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7
