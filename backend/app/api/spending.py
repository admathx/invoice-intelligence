"""What a location spends, month by month, by category and by distributor:
the food-cost view an owner watches.

Sums the invoice items (before tax), so the category and distributor
breakdowns add up to the month's total exactly. Only invoices whose numbers
are trusted count ("extracted" or "confirmed"), the same rule as every other
figure in the app: one that needs a look is left out until someone fixes it,
and the page says how many that is. Months are the invoice date's.
"""
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import get_tenant_or_404
from app.auth import get_db_for_tenant
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import InvoiceStatus

router = APIRouter(prefix="/spending", tags=["spending"])

MAX_MONTHS = 12
UNMATCHED = "Not matched yet"
_COUNTED = (InvoiceStatus.extracted, InvoiceStatus.confirmed)


class MonthSpend(BaseModel):
    month: str  # "2026-08"
    total: Decimal
    invoice_count: int
    by_category: dict[str, Decimal]
    by_distributor: dict[str, Decimal]


class SpendingOut(BaseModel):
    # Oldest first: from the first month with an invoice (at most MAX_MONTHS
    # back) through the current month, empty months included, so a gap reads
    # as a gap rather than disappearing.
    months: list[MonthSpend]
    # Invoices left out because their numbers aren't trusted yet.
    not_counted: int


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def _as_date(value) -> date:
    # date_trunc on a date column comes back as a timestamp.
    return value.date() if hasattr(value, "date") else value


def _category_label(category: str | None) -> str:
    return category.capitalize() if category else UNMATCHED


@router.get("", response_model=SpendingOut)
def spending(tenant_id: uuid.UUID, today: date | None = None, db: Session = Depends(get_db_for_tenant)) -> SpendingOut:
    get_tenant_or_404(db, tenant_id)
    current = _month_start(today or date.today())
    earliest_allowed = _add_months(current, -(MAX_MONTHS - 1))

    month = func.date_trunc("month", Invoice.invoice_date)
    counted = (
        Invoice.tenant_id == tenant_id,
        Invoice.status.in_(_COUNTED),
        Invoice.invoice_date >= earliest_allowed,
        Invoice.invoice_date < _add_months(current, 1),
    )
    rows = db.execute(
        select(
            month,
            CanonicalSku.category,
            Distributor.name,
            Distributor.slug,
            func.sum(InvoiceLineItem.extended_price),
        )
        .join(InvoiceLineItem, InvoiceLineItem.invoice_id == Invoice.id)
        .outerjoin(CanonicalSku, CanonicalSku.id == InvoiceLineItem.canonical_sku_id)
        .outerjoin(Distributor, Distributor.id == Invoice.distributor_id)
        .where(*counted)
        .group_by(month, CanonicalSku.category, Distributor.name, Distributor.slug)
    ).all()
    invoice_counts = {
        _as_date(m): n for m, n in db.execute(select(month, func.count(Invoice.id)).where(*counted).group_by(month))
    }
    not_counted = db.scalar(
        select(func.count(Invoice.id)).where(
            Invoice.tenant_id == tenant_id,
            Invoice.status.in_([InvoiceStatus.needs_review, InvoiceStatus.failed]),
        )
    )

    by_month: dict[date, dict] = defaultdict(
        lambda: {"total": Decimal("0"), "category": defaultdict(Decimal), "distributor": defaultdict(Decimal)}
    )
    for month_start, category, distributor, slug, amount in rows:
        bucket = by_month[_as_date(month_start)]
        bucket["total"] += amount
        bucket["category"][_category_label(category)] += amount
        known = distributor if distributor and slug != UNRECOGNIZED_SLUG else "Distributor not known"
        bucket["distributor"][known] += amount

    first = min(by_month, default=current)
    start = max(_month_start(first), earliest_allowed)
    months: list[MonthSpend] = []
    m = start
    while m <= current:
        bucket = by_month.get(m)
        months.append(
            MonthSpend(
                month=f"{m:%Y-%m}",
                total=(bucket["total"] if bucket else Decimal("0")).quantize(Decimal("0.01")),
                invoice_count=invoice_counts.get(m, 0),
                by_category={k: v.quantize(Decimal("0.01")) for k, v in (bucket["category"] if bucket else {}).items()},
                by_distributor={
                    k: v.quantize(Decimal("0.01")) for k, v in (bucket["distributor"] if bucket else {}).items()
                },
            )
        )
        m = _add_months(m, 1)
    return SpendingOut(months=months, not_counted=not_counted)
