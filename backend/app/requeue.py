"""Picking up invoices whose extraction job was lost.

An invoice waits in received / rendering / extracting while its job runs.
If the job never got queued (Redis was down at the moment of upload, or when
email intake recorded it), or the worker died mid-job (a redeploy, running
out of memory), nothing would ever touch it again: "Reading" forever. The
scheduler runs requeue_stalled every few minutes; any in-progress invoice
with no live job gets queued again. The worker's retry is safe: it clears
whatever an interrupted attempt left (app/workers/tasks.py).

After MAX_REQUEUES an invoice is marked failed instead, with its reason, and
whoever runs the service is told: a file that crashes the worker every time
mustn't be retried (and billed) forever.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit, ops
from app.db import TENANT_SCOPE_BYPASS
from app.models import AuditEvent, Invoice
from app.models.enums import InvoiceStatus

logger = logging.getLogger(__name__)

IN_PROGRESS = (InvoiceStatus.received, InvoiceStatus.rendering, InvoiceStatus.extracting)
# Long enough that an upload that has just committed has had its chance to
# queue itself; well short of anyone giving up on it.
GRACE = timedelta(minutes=5)
MAX_REQUEUES = 3
REQUEUED = "invoice.requeued"
_LIVE_JOB_STATES = {"queued", "started", "deferred", "scheduled"}


def _job_is_alive(invoice_id: uuid.UUID) -> bool:
    from rq.exceptions import NoSuchJobError
    from rq.job import Job

    from app.queue import job_id_for, redis_conn

    try:
        status = Job.fetch(job_id_for(invoice_id), connection=redis_conn).get_status(refresh=True)
    except NoSuchJobError:
        return False
    return (status.value if hasattr(status, "value") else str(status)) in _LIVE_JOB_STATES


def requeue_stalled(
    db: Session, now: datetime | None = None, *, tenant_ids: list[uuid.UUID] | None = None
) -> dict[str, int]:
    """Counts of what it did: {"requeued": n, "gave_up": n}. tenant_ids
    limits it to those locations (tests; the scheduler does them all)."""
    from app.queue import enqueue_extraction

    now = now or datetime.now(timezone.utc)
    done = {"requeued": 0, "gave_up": 0}
    query = select(Invoice.id, Invoice.tenant_id).where(Invoice.status.in_(IN_PROGRESS), Invoice.created_at < now - GRACE)
    if tenant_ids is not None:
        query = query.where(Invoice.tenant_id.in_(tenant_ids))
    stalled = db.execute(query.execution_options(**{TENANT_SCOPE_BYPASS: True})).all()
    for invoice_id, tenant_id in stalled:
        if _job_is_alive(invoice_id):
            continue
        tries = db.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.entity_id == invoice_id, AuditEvent.action == REQUEUED)
        )
        if tries >= MAX_REQUEUES:
            invoice = db.get(Invoice, invoice_id, execution_options={TENANT_SCOPE_BYPASS: True})
            invoice.status = InvoiceStatus.failed
            audit.record(
                db, None, "invoice.extraction_failed", "invoice", invoice_id, tenant_id,
                error=f"stopped after {MAX_REQUEUES} tries: the job kept being lost",
            )  # fmt: skip
            db.commit()
            ops.alert(
                f"requeue:gave-up:{invoice_id}",
                "An invoice couldn't be read after several tries",
                f"Invoice {invoice_id} kept losing its job (the worker may be crashing on it). It's now marked "
                "'Couldn't read' so someone can type it in.",
            )
            done["gave_up"] += 1
            continue
        try:
            enqueue_extraction(invoice_id)
        except Exception as exc:
            ops.alert("requeue:queue-down", "Invoices can't be queued for reading", exc=exc)
            break  # the queue is down: every other one would fail the same way
        audit.record(db, None, REQUEUED, "invoice", invoice_id, tenant_id, attempt=tries + 1)
        db.commit()
        done["requeued"] += 1
    if any(done.values()):
        logger.info("stalled invoices: %s", done)
    return done
