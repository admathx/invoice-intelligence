"""A year of a location's buying at today's prices, and what would change
it: the numbers the Costs page plans with (app/analytics/costs.py)."""
import uuid
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analytics.costs import build_costs
from app.api.deps import get_tenant_or_404
from app.auth import get_db_for_tenant

router = APIRouter(prefix="/costs", tags=["costs"])


class ProductCostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    canonical_sku_id: uuid.UUID
    name: str
    category: str
    distributor: str
    yearly_cost: Decimal
    # How its price moved over the window (0.05 is 5% up); None if bought once.
    recent_change: Decimal | None


class ScenarioOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # "increases_reversed" | "cheaper_elsewhere" | "savings_targets" | "rise_again"
    key: str
    # Against yearly_cost: negative is a saving.
    yearly_change: Decimal
    products: int


class CostsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # The invoices the year is projected from; null with no history yet.
    window_start: date | None
    window_end: date | None
    window_days: int
    yearly_cost: Decimal
    # The part of the window's spending these products are, 0..1.
    coverage: Decimal | None
    # Dearest first.
    products: list[ProductCostOut]
    scenarios: list[ScenarioOut]


@router.get("", response_model=CostsOut)
def get_costs(tenant_id: uuid.UUID, db: Session = Depends(get_db_for_tenant)) -> CostsOut:
    tenant = get_tenant_or_404(db, tenant_id)
    return CostsOut.model_validate(build_costs(db, tenant, date.today()))
