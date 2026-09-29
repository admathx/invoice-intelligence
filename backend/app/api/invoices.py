import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import get_tenant_or_404
from app.api.invoice_review import build_invoice_detail
from app.auth import current_user, get_db_for_tenant
from app.config import settings
from app.ingest.upload import InvalidInvoiceFileError, invoice_pdf_from_upload, save_invoice_bytes
from app.models import Invoice, User
from app.models.enums import InvoiceSource, InvoiceStatus
from app.queue import enqueue_extraction
from app.schemas.invoices import InvoiceDetailOut, InvoiceOut, InvoiceUploadResponse
from app.storage import forget_original, get_storage, render_key

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("", response_model=InvoiceUploadResponse, status_code=201)
def upload_invoice(
    tenant_id: uuid.UUID,
    file: list[UploadFile],
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceUploadResponse:
    """One invoice: a PDF, or photos of a paper invoice, a page each, sent as
    several `file` parts in page order (app/ingest/upload.py)."""
    # Before anything is written to disk: an unknown tenant_id would otherwise
    # fail on the FK at flush time, after the bytes landed.
    get_tenant_or_404(db, tenant_id)

    # Read one byte past what's left of the limit rather than whole streams,
    # so an oversized upload is refused without first being pulled into memory.
    parts: list[bytes] = []
    budget = settings.max_upload_bytes + 1
    for upload in file:
        data = upload.file.read(budget)
        parts.append(data)
        budget -= len(data)
        if budget <= 0:
            break
    try:
        pdf, from_photos = invoice_pdf_from_upload(parts)
    except InvalidInvoiceFileError as exc:
        raise HTTPException(status_code=413 if exc.too_large else 415, detail=str(exc)) from exc

    invoice = Invoice(
        tenant_id=tenant_id,
        source=InvoiceSource.photo if from_photos else InvoiceSource.upload,
        status=InvoiceStatus.received,
        original_file_uri="",
    )
    db.add(invoice)
    db.flush()

    invoice_id = invoice.id  # still needed after a rollback discards the row
    try:
        filenames = [upload.filename for upload in file]
        invoice.original_file_uri = save_invoice_bytes(invoice_id, filenames[0], pdf)
        details = {"filename": filenames[0], "size_bytes": len(pdf)}
        if from_photos:
            details.update(filenames=filenames, photo_count=len(parts))
        audit.record(db, user, "invoice.uploaded", "invoice", invoice.id, tenant_id, **details)
        db.commit()
    except Exception:
        # The file is stored before the row commits; if the row doesn't make
        # it, neither should the file, or it sits in storage unreferenced.
        db.rollback()
        forget_original(invoice_id)
        raise
    db.refresh(invoice)

    enqueue_extraction(invoice.id)

    return InvoiceUploadResponse(id=invoice.id, status=invoice.status)


@router.get("", response_model=list[InvoiceOut])
def list_invoices(tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)) -> list[Invoice]:
    get_tenant_or_404(db, tenant_id)
    # The tenant_id filter here is redundant with the auto-injected TenantScoped
    # criteria from get_db_for_tenant (defense in depth), not a substitute for it.
    return list(db.scalars(select(Invoice).where(Invoice.tenant_id == tenant_id).order_by(Invoice.created_at.desc())))


@router.get("/{invoice_id}", response_model=InvoiceDetailOut)
def get_invoice(
    invoice_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> InvoiceDetailOut:
    get_tenant_or_404(db, tenant_id)
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise HTTPException(status_code=404, detail="We couldn't find that invoice.")
    # Shared with the review endpoints, so the detail page always carries the
    # same arithmetic check the confirm endpoint will enforce.
    return build_invoice_detail(db, invoice)


# Exactly what app/ingest/render.py writes. Anything else (a path separator,
# "..") is refused before it gets near the filesystem.
_PAGE_NAME = re.compile(r"page_\d{3}\.png")


@router.get("/{invoice_id}/pages/{page_name}")
def get_page_image(
    invoice_id: uuid.UUID, page_name: str, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)
) -> Response:
    """A rendered page of the invoice, for the review screen. Authorized like
    every other read of the invoice: the tenant-scoped lookup below is what
    makes another location's invoice id a 404."""
    get_tenant_or_404(db, tenant_id)
    if db.get(Invoice, invoice_id) is None or not _PAGE_NAME.fullmatch(page_name):
        raise HTTPException(status_code=404, detail="page not found")
    data = get_storage().get(render_key(invoice_id, page_name))
    if data is None:
        raise HTTPException(status_code=404, detail="page not found")
    # Not cached anywhere, the browser included: a restaurant's office PC is
    # often shared, and signing out has to take the invoices with it.
    return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})
