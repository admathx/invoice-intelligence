"""Sends the scheduled emails: price-increase alerts within minutes of an
alert opening (app/alert_emails.py), and the weekly digest when it's due.
Runs for as long as the deployment does (the `scheduler` service in
deploy/docker-compose.prod.yml).

Checks every few minutes. Each check emails any new big price increases, and
once this week's send time (settings.digest_weekday at digest_hour_utc) has
passed, the digest to everyone not yet sent it. Both are idempotent (per
alert per person, and per person per week), so restarts, deploys and a failed
send retried on the next check are all safe; there is no "missed the window"
state, only "not sent yet".
"""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import alert_emails, digest  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402

CHECK_EVERY_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("digest_scheduler")


def send_digests(now: datetime) -> None:
    if not settings.digests_enabled or not digest.is_due(now):
        return
    db = SessionLocal()
    try:
        run = digest.send_due_digests(db, now)
    finally:
        db.close()
    if run.sent or run.failed:
        log.info("week of %s: sent %d, quiet %d, failed %d", run.week_of, run.sent, run.quiet, len(run.failed))
    for failure in run.failed:
        log.warning("digest failed: %s", failure)


def send_alert_emails(now: datetime) -> None:
    if not settings.alert_emails_enabled:
        return
    db = SessionLocal()
    try:
        run = alert_emails.send_alert_emails(db, now)
    finally:
        db.close()
    if run.sent or run.failed:
        log.info("price-increase emails: sent %d covering %d alerts, failed %d", run.sent, run.alerts, len(run.failed))
    for failure in run.failed:
        log.warning("price-increase email failed: %s", failure)


def tick() -> None:
    now = datetime.now(timezone.utc)
    # Each on its own: a failure in one mustn't hold up the other.
    for job in (send_alert_emails, send_digests):
        try:
            job(now)
        except Exception:  # a database blip mustn't end the scheduler
            log.exception("%s failed; retrying next time", job.__name__)


if __name__ == "__main__":
    log.info(
        "email scheduler: digest weekday %d, %02d:00 UTC; price-increase emails %s",
        settings.digest_weekday,
        settings.digest_hour_utc,
        "on" if settings.alert_emails_enabled else "off",
    )
    while True:
        tick()
        time.sleep(CHECK_EVERY_SECONDS)
