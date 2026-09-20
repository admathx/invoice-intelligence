import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.db import get_db, get_db_for_tenant
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem
from app.schemas.skus import CanonicalSkuDetail, CanonicalSkuSearchResult, SkuMatchedLine

router = APIRouter(prefix="/skus", tags=["skus"])


@router.get("", response_model=list[CanonicalSkuSearchResult])
def search_skus(q: str, db: Session = Depends(get_db)) -> list[CanonicalSku]:
    # canonical_skus is a shared reference table (not TenantScoped, see its
    # model docstring) — search isn't scoped to a tenant, only the matched-line
    # detail view below is.
    if not q.strip():
        return []
    return list(db.scalars(select(CanonicalSku).where(CanonicalSku.name.ilike(f"%{q}%")).limit(25)))


@router.get("/{sku_id}", response_model=CanonicalSkuDetail)
def get_sku(sku_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)) -> CanonicalSkuDetail:
    get_tenant_or_404(db, tenant_id)
    sku = db.get(CanonicalSku, sku_id)
    if sku is None:
        raise HTTPException(status_code=404, detail="canonical SKU not found")

    rows = db.execute(
        select(InvoiceLineItem, Invoice, Distributor)
        .join(Invoice, InvoiceLineItem.invoice_id == Invoice.id)
        .join(Distributor, Invoice.distributor_id == Distributor.id)
        .where(InvoiceLineItem.canonical_sku_id == sku_id)
        .order_by(Invoice.invoice_date.desc())
        .limit(100)
    ).all()

    matched_lines = [
        SkuMatchedLine(
            invoice_id=invoice.id,
            invoice_date=invoice.invoice_date,
            distributor_name=distributor.name,
            raw_description=line.raw_description,
            normalized_unit_price=line.normalized_unit_price,
            match_confidence=line.match_confidence,
            review_status=line.review_status.value,
        )
        for line, invoice, distributor in rows
    ]

    return CanonicalSkuDetail(
        id=sku.id,
        name=sku.name,
        category=sku.category,
        subcategory=sku.subcategory,
        base_uom=sku.base_uom.value,
        matched_lines=matched_lines,
    )
