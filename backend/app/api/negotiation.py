"""SPEC.md §9: the negotiation sheet — the ranked table, printable."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.analytics.negotiation import build_negotiation_sheet
from app.db import get_db_for_tenant
from app.models import CanonicalSku
from app.schemas.negotiation import NegotiationLineOut

router = APIRouter(prefix="/negotiation", tags=["negotiation"])


@router.get("", response_model=list[NegotiationLineOut])
def get_negotiation_sheet(
    tenant_id: uuid.UUID, as_of: date | None = None, db: Session = Depends(get_db_for_tenant)
) -> list[NegotiationLineOut]:
    sheet = build_negotiation_sheet(db, tenant_id, as_of or date.today())
    names = {sku.id: sku.name for sku in (db.get(CanonicalSku, line.canonical_sku_id) for line in sheet) if sku}
    return [
        NegotiationLineOut(
            canonical_sku_id=line.canonical_sku_id,
            canonical_sku_name=names.get(line.canonical_sku_id, "(unknown SKU)"),
            current_price=line.current_price,
            current_price_line_item_id=line.current_price_line_item_id,
            target_price=line.target_price,
            peer_distinct_tenant_count=line.peer_distinct_tenant_count,
            trailing_90d_quantity=line.trailing_90d_quantity,
            quantity_line_item_ids=line.quantity_line_item_ids,
            recoverable_90d=line.recoverable_90d,
            annualized_savings=line.annualized_savings,
        )
        for line in sheet
    ]
