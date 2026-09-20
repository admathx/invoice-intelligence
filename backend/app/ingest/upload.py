import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings


def save_invoice_bytes(invoice_id: uuid.UUID, filename: str | None, data: bytes) -> str:
    """Persist invoice bytes to disk and return their storage URI.

    Shared by the HTTP upload path and the email watch directory
    (app/ingest/email_stub.py) so both name and place files identically —
    app/workers/tasks.py's renderer resolves whatever URI lands here.
    """
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename or "invoice.pdf").suffix or ".pdf"
    dest = upload_dir / f"{invoice_id}{suffix}"
    dest.write_bytes(data)

    return f"file://{dest.resolve()}"


def save_uploaded_file(invoice_id: uuid.UUID, upload: UploadFile) -> str:
    """Persist an uploaded file to disk and return its storage URI."""
    return save_invoice_bytes(invoice_id, upload.filename, upload.file.read())
