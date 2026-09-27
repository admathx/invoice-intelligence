"""Shared pytest fixtures.

`db_session` wraps each test in an outer transaction + SAVEPOINT that's
always rolled back at teardown, so a test's own `db.commit()` calls (needed
since production code paths call `.commit()` internally — e.g. `bind_tenant`
callers, `upsert_creep_alerts`) don't escape the wrapper. Without this, every
row a test commits (Tenant, Invoice, PriceObservation, ...) is permanent in
the dev database — code review on Phase 4 found this already happening
(orphaned "test-<hash>" distributors and duplicate "Test Tenant" rows
accumulated from tests using a bare `SessionLocal()` with only `.close()` in
teardown).
"""
import pytest
from sqlalchemy.orm import Session

from app.db import engine


@pytest.fixture()
def db_session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# --- Signed-in user for API tests -------------------------------------------
#
# Every tenant endpoint now requires a signed-in user. The existing API tests
# are about what the endpoints do, not who may call them, so each runs as a
# real operator row (audit events and sku_aliases.confirmed_by_user_id point
# at users.id, so it has to exist), injected by overriding current_user.
# tests/test_auth.py switches this off to exercise real sign-in, cookies,
# CSRF and membership checks end to end.

TEST_OPERATOR_EMAIL = "pytest-operator@test.invalid"


@pytest.fixture(scope="session")
def test_operator():
    import uuid
    from datetime import datetime, timezone

    from sqlalchemy import delete, or_, select
    from sqlalchemy.exc import IntegrityError

    from app.db import SessionLocal
    from app.models import AuditEvent, User

    db = SessionLocal()
    started = datetime.now(timezone.utc)
    user = db.scalar(select(User).where(User.email == TEST_OPERATOR_EMAIL))
    if user is None:
        # An unusable hash: this user is only ever injected, never signs in.
        user = User(id=uuid.uuid4(), email=TEST_OPERATOR_EMAIL, name="Pytest Operator", password_hash="!", is_operator=True)
        db.add(user)
        db.commit()
    db.refresh(user)
    db.expunge(user)
    try:
        yield user
    finally:
        # Events this run wrote: the operator's own, and system events (the
        # worker, email intake) whose tenant the tests have since deleted.
        # Bounded by the run's start so a dev server's events are left alone.
        db.execute(
            delete(AuditEvent).where(
                AuditEvent.occurred_at >= started,
                or_(
                    AuditEvent.actor_user_id == user.id,
                    AuditEvent.actor_user_id.is_(None) & AuditEvent.tenant_id.is_(None),
                ),
            )
        )
        db.commit()
        # And the operator itself, so it doesn't sit in the Users screen. Kept
        # instead if something it confirmed survived the run (an alias's
        # confirmed_by_user_id has no ON DELETE).
        try:
            db.execute(delete(User).where(User.id == user.id))
            db.commit()
        except IntegrityError:
            db.rollback()
        db.close()


@pytest.fixture(autouse=True)
def signed_in_as_operator(request, test_operator):
    from app.auth import current_user
    from app.main import app

    if request.node.get_closest_marker("real_auth"):
        yield
        return
    app.dependency_overrides[current_user] = lambda: test_operator
    try:
        yield
    finally:
        app.dependency_overrides.pop(current_user, None)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: use real sign-in instead of the injected test operator")
