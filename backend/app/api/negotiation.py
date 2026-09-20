"""SPEC.md §9: the negotiation sheet — the ranked table, printable."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.negotiation import NegotiationBasis, build_negotiation_sheet
from app.api.deps import get_tenant_or_404
from app.db import get_db_for_tenant
from app.models import CanonicalSku
from app.schemas.negotiation import NegotiationLineOut, NegotiationSheetOut

router = APIRouter(prefix="/negotiation", tags=["negotiation"])


@router.get("", response_model=NegotiationSheetOut)
def get_negotiation_sheet(
    tenant_id: uuid.UUID,
    as_of: date | None = None,
    basis: NegotiationBasis = NegotiationBasis.auto,
    db: Session = Depends(get_db_for_tenant),
) -> NegotiationSheetOut:
    get_tenant_or_404(db, tenant_id)
    sheet = build_negotiation_sheet(db, tenant_id, as_of or date.today(), basis)

    # One query for every SKU name on the sheet, not one db.get() per line —
    # TOP_N caps this at 15 today, but the pattern shouldn't get worse if
    # that ever grows (this is a "printable" sheet, not necessarily fixed
    # at 15 forever).
    sku_ids = {line.canonical_sku_id for line in sheet.lines}
    names = {sku.id: sku.name for sku in db.scalars(select(CanonicalSku).where(CanonicalSku.id.in_(sku_ids)))}

    return NegotiationSheetOut(
        lines=[
            NegotiationLineOut(
                canonical_sku_id=line.canonical_sku_id,
                canonical_sku_name=names.get(line.canonical_sku_id, "(unknown SKU)"),
                basis=line.basis.value,
                current_price=line.current_price,
                current_price_line_item_id=line.current_price_line_item_id,
                target_price=line.target_price,
                peer_account_count=line.peer_account_count,
                history_observation_count=line.history_observation_count,
                trailing_quantity=line.trailing_quantity,
                quantity_line_item_ids=line.quantity_line_item_ids,
                recoverable_in_window=line.recoverable_in_window,
                annualized_savings=line.annualized_savings,
            )
            for line in sheet.lines
        ],
        window_days=sheet.window_days,
        annualization_factor=sheet.annualization_factor,
        total_annualized_savings=sheet.total_annualized_savings,
    )
