"""One lock for every use of pypdfium2 in a process.

PDFium isn't thread-safe: two threads opening or rendering PDFs at once can
crash the whole process (the parallel test-set read segfaulted on exactly
that). The API runs sync endpoints on a thread pool, so two uploads at once
(or an upload and an inbound email) are two threads. Every pdfium call goes
through this lock; the work under it is quick next to everything around it.
Separate processes (the worker, the API) each have their own pdfium and
don't need to share it.
"""
import threading

# Reentrant: a caller holding it may call another function that takes it.
PDFIUM_LOCK = threading.RLock()
