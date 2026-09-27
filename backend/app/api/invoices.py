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
from app.ingest.upload import InvalidInvoiceFileError, save_invoice_bytes, validate_invoice_bytes
from app.models import Invoice, User
from app.models.enums import InvoiceSource, InvoiceStatus
from app.queue import invoice_queue as queue
from app.schemas.invoices import InvoiceDetailOut, InvoiceOut, InvoiceUploadResponse
from app.storage import get_storage, render_key
from app.workers.tasks import process_invoice

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("", response_model=InvoiceUploadResponse, status_code=201)
def upload_invoice(
    tenant_id: uuid.UUID,
    file: UploadFile,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> InvoiceUploadResponse:
    # Before anything is written to disk: an unknown tenant_id would otherwise
    # fail on the FK at flush time, after the bytes landed.
    get_tenant_or_404(db, tenant_id)

    # Read one byte past the limit rather than the whole stream, so an
    # oversized upload is refused without first being pulled into memory.
    data = file.file.read(settings.max_upload_bytes + 1)
    try:
        validate_invoice_bytes(data)
    except InvalidInvoiceFileError as exc:
        raise HTTPException(status_code=413 if exc.too_large else 415, detail=str(exc)) from exc

    invoice = Invoice(
        tenant_id=tenant_id,
        source=InvoiceSource.upload,
        status=InvoiceStatus.received,
        original_file_uri="",
    )
    db.add(invoice)
    db.flush()

    invoice.original_file_uri = save_invoice_bytes(invoice.id, file.filename, data)
    audit.record(
        db, user, "invoice.uploaded", "invoice", invoice.id, tenant_id, filename=file.filename, size_bytes=len(data)
    )
    db.commit()
    db.refresh(invoice)

    queue.enqueue(process_invoice, str(invoice.id))

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
        raise HTTPException(status_code=404, detail="invoice not found")
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
