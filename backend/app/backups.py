"""Keeping an eye on the database backups (deploy/backup.sh writes them).

Run by the scheduler about once an hour:
- check(): warns (app.ops) when the newest backup is older than
  settings.backup_max_age_hours, or there's none: backups that quietly stop
  are only found out about on the day they're needed.
- copy_offsite(): with S3 storage, copies each new backup to the bucket
  (under backups/), so losing the server doesn't lose them too. Keeps the
  newest OFFSITE_KEEP there.

Both do nothing unless settings.backup_dir is set (the production compose
file sets it; development has no backup service).
"""
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import ops
from app.config import settings
from app.storage import get_storage

logger = logging.getLogger(__name__)

OFFSITE_PREFIX = "backups/"
OFFSITE_KEEP = 30


def _dumps() -> list[Path]:
    directory = Path(settings.backup_dir)
    return sorted(directory.glob("invoice-*.dump"), key=lambda p: p.stat().st_mtime) if directory.is_dir() else []


def check(now: datetime | None = None, *, just_started: bool = False) -> str | None:
    """The problem found, if any (also alerted). just_started: the service
    has only just come up, so "no backups at all" is a new deployment whose
    first one is still being made, not a problem yet."""
    if not settings.backup_dir:
        return None
    now = now or datetime.now(timezone.utc)
    dumps = _dumps()
    if not dumps:
        if just_started:
            return None
        problem = f"No database backups in {settings.backup_dir}"
    else:
        newest = datetime.fromtimestamp(dumps[-1].stat().st_mtime, timezone.utc)
        age = now - newest
        if age <= timedelta(hours=settings.backup_max_age_hours):
            return None
        problem = f"The newest database backup is {int(age.total_seconds() // 3600)} hours old ({dumps[-1].name})"
    ops.alert("backups:stale", problem, "Check the backup service: docker compose ... logs backup")
    return problem


def copy_offsite() -> list[str]:
    """Names copied this time."""
    if not settings.backup_dir or settings.storage_backend != "s3":
        return []
    storage = get_storage()
    already = {key.rsplit("/", 1)[-1] for key in storage.list(OFFSITE_PREFIX)}
    copied = []
    # Only the newest few: an older one copied now would be trimmed straight
    # back off below, and copied again next hour.
    for dump in _dumps()[-OFFSITE_KEEP:]:
        if dump.name not in already:
            storage.put_file(f"{OFFSITE_PREFIX}{dump.name}", dump)
            copied.append(dump.name)
    # Oldest first by name (they're named by date), trimmed to the newest few.
    offsite = sorted(storage.list(OFFSITE_PREFIX))
    for key in offsite[:-OFFSITE_KEEP]:
        storage.delete(key)
    if copied:
        logger.info("copied backups offsite: %s", ", ".join(copied))
    return copied
