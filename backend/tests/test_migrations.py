"""The migration chain builds the schema from an empty database.

It didn't: migration 0001 used to call create_all() on the current models,
so on an empty database step 1 created everything and 0004 then failed
adding a column that already existed. The development database had only
ever been migrated forward, so nothing noticed until the first production
build. This builds a throwaway database from nothing on every run and checks
the result is exactly what the models describe.
"""
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import BACKEND_DIR, settings
from app.db import Base
import app.models  # noqa: F401  (registers every table on Base.metadata)


@pytest.fixture()
def empty_database(monkeypatch):
    name = f"ii_migration_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(settings.database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(settings.database_url).set(database=name).render_as_string(hide_password=False)
    # alembic/env.py takes its URL from settings.
    monkeypatch.setattr(settings, "database_url", url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return config


def _drift(url: str) -> list:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()


def test_migrations_build_the_models_schema_from_an_empty_database(empty_database):
    command.upgrade(_alembic(), "head")
    assert _drift(empty_database) == []


def test_every_migration_can_be_undone_and_redone(empty_database):
    config = _alembic()
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert _drift(empty_database) == []
