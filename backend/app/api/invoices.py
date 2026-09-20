import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from redis import Redis
from rq import Queue
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.config import settings
from app.db import get_db_for_tenant
from app.ingest.upload import save_uploaded_file
from app.models import Invoice, InvoiceLineItem
from app.models.enums import InvoiceSource, InvoiceStatus
from app.schemas.invoices import InvoiceDetailOut, InvoiceOut, InvoiceUploadResponse, LineItemOut
from app.workers.tasks import process_invoice

router = APIRouter(prefix="/invoices", tags=["invoices"])

redis_conn = Redis.from_url(settings.redis_url)
queue = Queue("invoices", connection=redis_conn)


@router.post("", response_model=InvoiceUploadResponse, status_code=201)
def upload_invoice(
    tenant_id: uuid.UUID,
    file: UploadFile,
    db: Session = Depends(get_db_for_tenant),
) -> InvoiceUploadResponse:
    # Before save_uploaded_file writes anything to disk: an unknown tenant_id
    # would otherwise fail on the FK at flush time, after the bytes landed.
    get_tenant_or_404(db, tenant_id)

    invoice = Invoice(
        tenant_id=tenant_id,
        source=InvoiceSource.upload,
        status=InvoiceStatus.received,
        original_file_uri="",
    )
    db.add(invoice)
    db.flush()

    invoice.original_file_uri = save_uploaded_file(invoice.id, file)
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
    line_items = list(
        db.scalars(
            select(InvoiceLineItem)
            .where(InvoiceLineItem.invoice_id == invoice_id)
            .order_by(InvoiceLineItem.line_number)
        )
    )
    render_dir = Path(settings.upload_dir) / "renders" / str(invoice_id)
    page_image_urls = (
        [f"/renders/{invoice_id}/{p.name}" for p in sorted(render_dir.glob("page_*.png"))]
        if render_dir.is_dir()
        else []
    )
    return InvoiceDetailOut(
        **InvoiceOut.model_validate(invoice).model_dump(),
        line_items=[LineItemOut.model_validate(li) for li in line_items],
        page_image_urls=page_image_urls,
    )
