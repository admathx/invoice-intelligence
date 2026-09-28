"""Send this week's digest to everyone who hasn't had it (normally the
scheduler's job: scripts/digest_scheduler.py).

    python scripts/send_digests.py                  # send what's due now
    python scripts/send_digests.py --preview EMAIL  # write EMAIL's digest to the outbox, send nothing, record nothing

--preview always writes to the outbox directory (settings.outbox_dir), even
when MAIL_BACKEND=smtp, so you can see exactly what someone would get
without it reaching them. Sending is idempotent per person per week.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app import digest, mail  # noqa: E402
from app.auth import normalize_email  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402


def preview(db, email: str) -> str:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        return f"no user {email}"
    tenants = next((ts for u, ts in digest.recipients(db) if u.id == user.id), None)
    if tenants is None:
        return f"{user.email} gets no digest (no locations, inactive, or unsubscribed)"
    now = datetime.now(timezone.utc)
    message = digest.compose(user, [digest.location_week(db, t, now) for t in tenants])
    if message is None:
        return f"{user.email}: nothing to report this week, so no email"
    previous = settings.mail_backend
    settings.mail_backend = "outbox"
    try:
        mail.send(message)
    finally:
        settings.mail_backend = previous
    return f"{user.email}: '{message['Subject']}' written to {settings.outbox_dir}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", metavar="EMAIL")
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.preview:
            print(preview(db, args.preview))
            return 0
        run = digest.send_due_digests(db)
    finally:
        db.close()
    print(f"week of {run.week_of}: sent {run.sent}, nothing to report {run.quiet}, already sent {run.already_sent}")
    for failure in run.failed:
        print(f"  FAILED {failure}")
    return 1 if run.failed else 0


if __name__ == "__main__":
    sys.exit(main())
