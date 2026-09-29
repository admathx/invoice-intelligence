import uuid

from app.config import settings
from app.ingest.photos import MAX_PHOTOS, UnreadablePhotoError, image_kind, photos_to_pdf
from app.storage import get_storage, original_key

# The PDF header. Checking for it is what makes "this is a PDF" a fact about
# the bytes rather than about a filename or a Content-Type header, both of
# which the sender controls.
PDF_MAGIC = b"%PDF-"
# Where the header may sit. The spec puts it at byte 0, but readers (Acrobat's
# implementation notes, pdf.js, poppler) accept it anywhere in the first 1024
# bytes, and real files arrive with a BOM, a stray newline or a MIME remnant in
# front of it. Requiring byte 0 rejected invoices every viewer opens.
PDF_HEADER_WINDOW = 1024


def _megabytes(size: int) -> str:
    return f"{size // (1024 * 1024)} MB"


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
            f"That file is too big. The limit is {_megabytes(settings.max_upload_bytes)}.", too_large=True
        )
    if not is_pdf_bytes(data):
        raise InvalidInvoiceFileError("That file isn't a PDF.")


def invoice_pdf_from_upload(files: list[bytes]) -> tuple[bytes, bool]:
    """The PDF an upload becomes, and whether it came from photos.

    An upload is either one PDF, or up to MAX_PHOTOS photos that are the
    pages of one paper invoice (app/ingest/photos.py). Anything else is
    refused before an invoice row exists. The size limit applies to what was
    sent, all files together, and again to the PDF the photos became.
    """
    if not files:
        raise InvalidInvoiceFileError("Choose a file to upload.")
    sent = sum(len(data) for data in files)
    if sent > settings.max_upload_bytes:
        raise InvalidInvoiceFileError(
            f"That's too much to upload at once. The limit is {_megabytes(settings.max_upload_bytes)}.", too_large=True
        )

    kinds = [("pdf" if is_pdf_bytes(data) else image_kind(data)) for data in files]
    if kinds == ["pdf"]:
        validate_invoice_bytes(files[0])
        return files[0], False
    if "pdf" in kinds:
        raise InvalidInvoiceFileError("Upload a PDF on its own, without photos.")
    if None in kinds:
        which = kinds.index(None) + 1
        raise InvalidInvoiceFileError(
            "That file isn't a PDF or a photo."
            if len(files) == 1
            else f"File {which} isn't a PDF or a photo."
        )
    if len(files) > MAX_PHOTOS:
        raise InvalidInvoiceFileError(f"One invoice can have up to {MAX_PHOTOS} photos.")
    try:
        pdf = photos_to_pdf(files)
    except UnreadablePhotoError as exc:
        raise InvalidInvoiceFileError(str(exc)) from exc
    validate_invoice_bytes(pdf)
    return pdf, True


def save_invoice_bytes(invoice_id: uuid.UUID, filename: str | None, data: bytes) -> str:
    """Store an invoice's original file and return its storage URI.

    Shared by the HTTP upload path and email intake (app/ingest/email_stub.py)
    so both name and place files identically; app/workers/tasks.py reads back
    whatever URI lands here (app.storage.read_uri). Callers validate first
    (validate_invoice_bytes). The suffix is always .pdf: validation has
    established the bytes are one, whatever the sender called the file.
    """
    return get_storage().put(original_key(invoice_id, ".pdf"), data)
