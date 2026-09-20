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
