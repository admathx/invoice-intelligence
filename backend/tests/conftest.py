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
