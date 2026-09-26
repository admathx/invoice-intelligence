import uuid
from pathlib import Path

from app.config import settings

# The PDF header. Checking for it is what makes "this is a PDF" a fact about
# the bytes rather than about a filename or a Content-Type header, both of
# which the sender controls.
PDF_MAGIC = b"%PDF-"
# Where the header may sit. The spec puts it at byte 0, but readers (Acrobat's
# implementation notes, pdf.js, poppler) accept it anywhere in the first 1024
# bytes, and real files arrive with a BOM, a stray newline or a MIME remnant in
# front of it. Requiring byte 0 rejected invoices every viewer opens.
PDF_HEADER_WINDOW = 1024


class InvalidInvoiceFileError(ValueError):
    """The bytes can't be an invoice this pipeline can process."""

    def __init__(self, message: str, *, too_large: bool = False) -> None:
        super().__init__(message)
        self.too_large = too_large


def is_pdf_bytes(data: bytes) -> bool:
    return PDF_MAGIC in data[:PDF_HEADER_WINDOW]


def validate_invoice_bytes(data: bytes) -> None:
    """Rejects what the worker can't use, before anything touches disk.

    Both entry points (HTTP upload and email) previously accepted any bytes: a
    renamed .docx or a multi-gigabyte file was written to uploads/, got an
    invoice row, was enqueued, and only then failed in the renderer, leaving
    the file behind and a `failed` invoice on the dashboard.
    """
    if len(data) > settings.max_upload_bytes:
        raise InvalidInvoiceFileError(
            f"file is {len(data)} bytes; the limit is {settings.max_upload_bytes}", too_large=True
        )
    if not is_pdf_bytes(data):
        raise InvalidInvoiceFileError("file is not a PDF")


def save_invoice_bytes(invoice_id: uuid.UUID, filename: str | None, data: bytes) -> str:
    """Persist invoice bytes to disk and return their storage URI.

    Shared by the HTTP upload path and the email watch directory
    (app/ingest/email_stub.py) so both name and place files identically —
    app/workers/tasks.py's renderer resolves whatever URI lands here.
    Callers validate first (validate_invoice_bytes).
    """
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename or "invoice.pdf").suffix or ".pdf"
    dest = upload_dir / f"{invoice_id}{suffix}"
    dest.write_bytes(data)

    return f"file://{dest.resolve()}"
