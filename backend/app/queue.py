"""The one invoice-processing queue.

Both entry points that can create an invoice — the HTTP upload endpoint
(app/api/invoices.py) and the email watch directory
(app/ingest/email_stub.py) — enqueue onto this same queue, rather than each
constructing their own Redis connection and Queue for the same "invoices"
name.
"""
from redis import Redis
from rq import Queue

from app.config import settings

redis_conn = Redis.from_url(settings.redis_url)
invoice_queue = Queue("invoices", connection=redis_conn)


def enqueue_extraction(invoice_id) -> None:
    """Queue one invoice for extraction, with a time limit that fits it.

    Without job_timeout, RQ applies 180 s and kills the job mid-stream on a
    large invoice, after the API calls were already paid for; the worker
    then marks the invoice failed.
    """
    from app.workers.tasks import process_invoice  # the worker module imports half the app

    invoice_queue.enqueue(process_invoice, str(invoice_id), job_timeout=settings.extraction_job_timeout_seconds)
