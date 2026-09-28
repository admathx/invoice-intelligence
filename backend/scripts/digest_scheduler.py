"""Sends the weekly digest when it's due. Runs for as long as the deployment
does (the `scheduler` service in deploy/docker-compose.prod.yml).

Checks every few minutes; once this week's send time (settings.digest_weekday
at digest_hour_utc) has passed, sends to everyone not yet sent to. Sending is
idempotent per person per week (app.digest), so restarts, deploys and a
failed send retried on the next check are all safe; there is no "missed the
window" state, only "not sent yet".
"""
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import digest  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402

CHECK_EVERY_SECONDS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("digest_scheduler")


def tick() -> None:
    now = datetime.now(timezone.utc)
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


if __name__ == "__main__":
    log.info("digest scheduler: weekday %d, %02d:00 UTC", settings.digest_weekday, settings.digest_hour_utc)
    while True:
        try:
            tick()
        except Exception:  # a database blip mustn't end the scheduler
            log.exception("digest check failed; retrying next time")
        time.sleep(CHECK_EVERY_SECONDS)
