"""Telling whoever runs the service that something is wrong, before a
customer does.

alert() logs the problem and, when settings.ops_email is set, emails it
there: at most once an hour for each kind of problem (its `key`), so a
crash loop sends one email, not thousands. The hourly throttle lives in
Redis, which the API, worker and scheduler all share; if Redis is the thing
that's down, the throttle falls back to this process's own memory rather
than going quiet.

What calls it: unhandled errors in the API (app/main.py), extraction jobs
that fail for a reason other than an unreadable invoice (an API key or
credit problem stops every invoice: app/workers/tasks.py), the scheduler
(scripts/digest_scheduler.py), errors people hit in the dashboard, and
database backups going stale (app/backups.py).
"""
import html
import logging
import time
import traceback

from app import email_design as design
from app import mail
from app.config import settings

logger = logging.getLogger("ops")

THROTTLE_SECONDS = 3600
_local_throttle: dict[str, float] = {}


def _first_this_hour(key: str) -> bool:
    try:
        from app.queue import redis_conn

        return bool(redis_conn.set(f"ops-alert:{key}", "1", nx=True, ex=THROTTLE_SECONDS))
    except Exception:
        now = time.monotonic()
        if now - _local_throttle.get(key, -THROTTLE_SECONDS) < THROTTLE_SECONDS:
            return False
        _local_throttle[key] = now
        return True


def alert(key: str, summary: str, detail: str = "", exc: BaseException | None = None) -> bool:
    """Report a problem. Returns whether an email went out. Never raises:
    reporting a problem mustn't become a second one."""
    trace = "".join(traceback.format_exception(exc)) if exc else ""
    logger.error("%s%s", summary, f"\n{detail}" if detail else "", exc_info=exc)
    if not settings.ops_email or not _first_this_hour(key):
        return False
    body_text = "\n\n".join(
        part
        for part in (
            summary,
            detail,
            trace,
            f"Seen on {settings.public_base_url}. Further alerts like this one are held back for an hour.",
        )
        if part
    )
    body_html = design.document(
        tag="Problem",
        body=(
            f"<p style='margin:0 0 8px;font-weight:700;color:{design.RED_TEXT}'>{html.escape(summary)}</p>"
            + (f"<p style='margin:0 0 8px;white-space:pre-wrap'>{html.escape(detail)}</p>" if detail else "")
            + (
                "<pre style='margin:0;padding:10px;background:#f3f4f6;border-radius:6px;font-size:12px;"
                f"white-space:pre-wrap;overflow-wrap:anywhere'>{html.escape(trace[-6000:])}</pre>"
                if trace
                else ""
            )
        ),
        footer=f"Seen on {html.escape(settings.public_base_url)}. Alerts like this one are held back for an hour.",
    )
    try:
        mail.send(
            mail.build_message(to=settings.ops_email, subject=f"[Invoice Intelligence] {summary}"[:200], text=body_text, html=body_html)
        )
        return True
    except Exception:
        logger.exception("couldn't email the ops alert %r", key)
        return False
