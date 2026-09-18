import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings


def save_uploaded_file(invoice_id: uuid.UUID, upload: UploadFile) -> str:
    """Persist an uploaded file to disk and return its storage URI."""
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(upload.filename or "invoice.pdf").suffix or ".pdf"
    dest = upload_dir / f"{invoice_id}{suffix}"
    with dest.open("wb") as f:
        f.write(upload.file.read())

    return f"file://{dest.resolve()}"
