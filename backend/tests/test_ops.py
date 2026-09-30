"""Monitoring: problem alerts (app/ops.py), the API's error handling and
health check, extraction failures that stop every invoice, and backups
(app/backups.py)."""
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy

import pytest
from fastapi.testclient import TestClient

from app import backups, ops
from app.auth import signed_in_user
from app.config import settings
from app.db import get_db
from app.main import app


@pytest.fixture()
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "mail_backend", "outbox")
    monkeypatch.setattr(settings, "outbox_dir", str(tmp_path / "outbox"))
    return tmp_path / "outbox"


@pytest.fixture()
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(ops, "alert", lambda key, summary, detail="", exc=None: seen.append((key, summary)) or True)
    return seen


def _key() -> str:
    return f"test:{uuid.uuid4().hex}"


# --- alert() ---------------------------------------------------------------------


def test_an_alert_is_emailed_once_an_hour_per_kind(outbox, monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")
    key = _key()
    try:
        raise RuntimeError("the database went away")
    except RuntimeError as exc:
        assert ops.alert(key, "Something broke", "while doing a thing", exc=exc) is True
    assert ops.alert(key, "Something broke") is False, "the same problem again within the hour"
    assert ops.alert(_key(), "Something else broke") is True

    sent = [message_from_bytes(p.read_bytes(), policy=policy.default) for p in outbox.glob("*.eml")]
    assert sorted(m["Subject"] for m in sent) == [
        "[Invoice Intelligence] Something broke",
        "[Invoice Intelligence] Something else broke",
    ]
    first = next(m for m in sent if m["Subject"].endswith("Something broke"))
    assert first["To"] == "ops@example.com"
    assert "the database went away" in first.get_body(("plain",)).get_content()


def test_without_an_ops_address_problems_are_only_logged(outbox, monkeypatch, caplog):
    import logging

    # Alembic's logging setup (tests/test_migrations.py runs it) switches
    # off loggers that already exist; the API never runs migrations itself.
    monkeypatch.setattr(logging.getLogger("ops"), "disabled", False)
    monkeypatch.setattr(settings, "ops_email", "")
    assert ops.alert(_key(), "Something broke") is False
    assert "Something broke" in caplog.text
    assert not outbox.exists()


def test_the_throttle_holds_even_without_redis(outbox, monkeypatch):
    monkeypatch.setattr(settings, "ops_email", "ops@example.com")

    class Down:
        def set(self, *a, **k):
            raise ConnectionError("redis is down")

    monkeypatch.setattr("app.queue.redis_conn", Down())
    key = _key()
    assert ops.alert(key, "Redis is down") is True
    assert ops.alert(key, "Redis is down") is False


# --- the API ----------------------------------------------------------------------------


def test_an_unexpected_error_gets_a_plain_answer_and_an_alert(calls):
    def broken():
        raise RuntimeError("connection reset")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken
    try:
        resp = TestClient(app, raise_server_exceptions=False).get("/skus?q=milk")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Something went wrong on our side. Try again in a moment."}
    assert calls == [("api:GET /skus:RuntimeError", "Error in GET /skus: RuntimeError")]


def test_the_health_check_says_whether_the_parts_answer():
    assert TestClient(app).get("/health/ready").json() == {"status": "ok"}


@pytest.mark.real_auth
def test_page_errors_are_reported_by_signed_in_people_only(calls, test_operator):
    from app.queue import redis_conn

    redis_conn.delete(f"ops-limit:client-error:{test_operator.id}")
    body = {"message": "Cannot read properties of undefined", "page": "/spending?x=1", "digest": "abc"}
    assert TestClient(app).post("/ops/client-error", json=body).status_code == 401
    app.dependency_overrides[signed_in_user] = lambda: test_operator
    try:
        assert TestClient(app).post("/ops/client-error", json=body).status_code == 204
    finally:
        app.dependency_overrides.pop(signed_in_user, None)
    assert calls == [("client:/spending", "A page broke for someone: /spending")]


@pytest.mark.real_auth
def test_one_person_cant_flood_the_ops_inbox_with_page_errors(calls, test_operator, monkeypatch):
    from app.queue import redis_conn

    redis_conn.delete(f"ops-limit:client-error:{test_operator.id}")
    app.dependency_overrides[signed_in_user] = lambda: test_operator
    try:
        client = TestClient(app)
        for n in range(20):
            body = {"message": f"error {n} at {uuid.uuid4()}", "page": f"/page-{n}"}
            assert client.post("/ops/client-error", json=body).status_code == 204
    finally:
        app.dependency_overrides.pop(signed_in_user, None)
        redis_conn.delete(f"ops-limit:client-error:{test_operator.id}")
    assert len(calls) == 5, "a few reports an hour per person, however the messages vary"


# --- extraction ---------------------------------------------------------------------------

from test_review_api import _upload, db_session, tenant  # noqa: E402,F401  (committed-data fixtures)


@pytest.mark.parametrize("service_problem", [True, False])
def test_extraction_failures_that_stop_every_invoice_are_alerted(db_session, tenant, calls, monkeypatch, service_problem):
    """A credit or API problem stops every invoice and needs someone; an
    invoice that can't be read is already waiting for a person to type in."""
    from app.extract.client import ExtractionFailedError
    from app.workers import tasks

    class Failing:
        def extract(self, pages):
            if service_problem:
                raise RuntimeError("Your credit balance is too low")
            raise ExtractionFailedError("didn't validate twice", cost_usd=0.01)

    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "extractor", Failing())
    invoice_id = _upload(tenant)
    with pytest.raises(Exception):
        tasks.process_invoice(str(invoice_id))
    if service_problem:
        assert calls == [("worker:RuntimeError", "Invoices aren't being read: RuntimeError")]
    else:
        assert calls == []


# --- backups ------------------------------------------------------------------------------


def _dump(directory, name, age_hours):
    path = directory / name
    path.write_bytes(b"PGDMP fake")
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


def test_stale_or_missing_backups_are_alerted(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    now = datetime.now(timezone.utc)
    assert backups.check(now) == f"No database backups in {tmp_path}"
    _dump(tmp_path, "invoice-20260901-0700.dump", 40)
    assert "hours old (invoice-20260901-0700.dump)" in backups.check(now)
    _dump(tmp_path, "invoice-20260902-0700.dump", 2)
    assert backups.check(now) is None
    assert [k for k, _ in calls] == ["backups:stale", "backups:stale"]


def test_no_backup_directory_means_nothing_to_check(monkeypatch, calls):
    monkeypatch.setattr(settings, "backup_dir", "")
    assert backups.check() is None and backups.copy_offsite() == []
    assert calls == []


def test_new_backups_are_copied_off_the_server_once(tmp_path, monkeypatch):
    import boto3
    from moto import mock_aws

    from app.storage import get_storage

    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(backups, "OFFSITE_KEEP", 2)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="ops-test")
        monkeypatch.setattr(settings, "storage_backend", "s3")
        monkeypatch.setattr(settings, "s3_bucket", "ops-test")
        monkeypatch.setattr(settings, "s3_prefix", "")
        monkeypatch.setattr(settings, "s3_region", "us-east-1")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        get_storage.cache_clear()
        try:
            for day in (1, 2, 3):
                _dump(tmp_path, f"invoice-2026090{day}-0700.dump", 72 - day * 24)
            # The newest two (OFFSITE_KEEP): an older one would only be
            # trimmed off again, and re-copied every hour.
            assert backups.copy_offsite() == ["invoice-20260902-0700.dump", "invoice-20260903-0700.dump"]
            assert backups.copy_offsite() == [], "already there"
            assert sorted(get_storage().list("backups/")) == [
                "backups/invoice-20260902-0700.dump",
                "backups/invoice-20260903-0700.dump",
            ], "only the newest few kept off the server"
        finally:
            get_storage.cache_clear()


def test_a_new_deployment_isnt_alarmed_while_its_first_backup_is_made(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path))
    assert backups.check(just_started=True) is None
    assert calls == []
    # Once it has been running a while, none at all is a problem.
    assert backups.check() is not None


def test_the_scheduler_checks_backups_on_its_first_pass_then_hourly(monkeypatch):
    import sys

    sys.path.insert(0, ".")
    from scripts import digest_scheduler as scheduler

    ran = []
    monkeypatch.setattr(scheduler.backups, "check", lambda now, just_started: ran.append(just_started))
    monkeypatch.setattr(scheduler.backups, "copy_offsite", lambda: None)
    monkeypatch.setattr(scheduler, "_last_backup_check", None)
    now = datetime.now(timezone.utc)
    scheduler.look_after_backups(now)
    scheduler.look_after_backups(now)  # within the hour: skipped
    assert ran == [True], "checked straight away, in its start-up grace"
