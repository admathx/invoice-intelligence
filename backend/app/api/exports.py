"""A location's invoices as a spreadsheet, for the accountant or the
bookkeeping software: one row per invoice, or one per line item.

CSV because every accounting package imports it and every spreadsheet opens
it. Written for Excel in particular, since that's where most of these land:
a UTF-8 byte-order mark (without it Excel reads "JALAPEÑO" as mojibake),
CRLF line endings, and text cells that would otherwise run as formulas
defused (see _text).

Every invoice is included, whatever its status, with the status in its own
column: an invoice still waiting for review is money that was spent all the
same, and leaving it out would make the export disagree with the paper.
"""
import csv
import io
import re
import uuid
from datetime import date
from decimal import Decimal
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import get_tenant_or_404
from app.auth import current_user, get_db_for_tenant
from app.config import settings
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, User

router = APIRouter(prefix="/exports", tags=["exports"])


class ExportKind(str, Enum):
    invoices = "invoices"
    line_items = "line-items"


_STATUS = {
    "received": "Processing",
    "rendering": "Processing",
    "extracting": "Processing",
    "extracted": "Read",
    "needs_review": "Needs review",
    "confirmed": "Confirmed",
    "failed": "Couldn't read",
}
_MATCH = {"auto": "Matched", "pending": "Waiting for review", "confirmed": "Confirmed", "corrected": "Corrected"}

# A spreadsheet runs a cell that starts with one of these as a formula, and a
# supplier's description is text nobody here wrote ("=HYPERLINK(...)" on an
# invoice would otherwise be a live link in the accountant's workbook).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _text(value: str | None) -> str:
    """Free text from an invoice or a person. Only text: amounts are ours,
    and a credit's "-5.00" must stay a number, not become "'-5.00"."""
    if not value:
        return ""
    return "'" + value if value.startswith(_FORMULA_START) else value


def _money(value: Decimal | None) -> str:
    return "" if value is None else str(value.quantize(Decimal("0.01")))


def _price(value: Decimal | None) -> str:
    """Unit prices keep the precision they were billed at ($0.5432/lb), and
    show at least cents."""
    if value is None:
        return ""
    cents = value.quantize(Decimal("0.01"))
    return str(cents) if cents == value else format(value.normalize(), "f")


def _quantity(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize(), "f")


def _date(value: date | None) -> str:
    return value.isoformat() if value else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "location"


def _invoice_link(invoice: Invoice) -> str:
    return f"{settings.public_base_url}/invoices/{invoice.id}?location={invoice.tenant_id}"


def _csv(header: list[str], rows: list[list[object]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return "﻿" + out.getvalue()


@router.get("/{kind}")
def export_csv(
    kind: ExportKind,
    tenant_id: uuid.UUID,
    start: date | None = None,
    end: date | None = None,
    distributor_id: uuid.UUID | None = None,
    db: Session = Depends(get_db_for_tenant),
    user: User = Depends(current_user),
) -> Response:
    """start/end are inclusive, on the invoice date. With either given, an
    invoice whose date couldn't be read is left out: there's no telling
    whether it belongs in the period."""
    tenant = get_tenant_or_404(db, tenant_id)
    if start and end and start > end:
        raise HTTPException(status_code=422, detail="the start date is after the end date")

    conditions = [Invoice.tenant_id == tenant_id]
    if start:
        conditions.append(Invoice.invoice_date >= start)
    if end:
        conditions.append(Invoice.invoice_date <= end)
    if distributor_id:
        conditions.append(Invoice.distributor_id == distributor_id)
    order = (Invoice.invoice_date.asc().nulls_last(), Invoice.created_at.asc())

    if kind == ExportKind.invoices:
        header = [
            "Location", "Invoice #", "Invoice date", "Delivery date", "Distributor",
            "Subtotal", "Tax", "Total", "Status", "Source", "Received", "Link",
        ]  # fmt: skip
        found = db.execute(
            select(Invoice, Distributor.name)
            .outerjoin(Distributor, Distributor.id == Invoice.distributor_id)
            .where(*conditions)
            .order_by(*order)
        ).all()
        rows = [
            [
                _text(tenant.name),
                _text(inv.invoice_number),
                _date(inv.invoice_date),
                _date(inv.delivery_date),
                _text(distributor),
                _money(inv.subtotal),
                _money(inv.tax),
                _money(inv.total),
                _STATUS.get(inv.status.value, inv.status.value),
                inv.source.value,
                _date(inv.created_at.date()),
                _invoice_link(inv),
            ]
            for inv, distributor in found
        ]
    else:
        header = [
            "Location", "Invoice #", "Invoice date", "Distributor", "Line", "Item code", "Description",
            "Pack size", "Quantity", "Unit", "Unit price", "Extended price", "Product", "Category",
            "Quantity (base unit)", "Base unit", "Price per base unit", "Match", "Invoice status", "Link",
        ]  # fmt: skip
        found = db.execute(
            select(InvoiceLineItem, Invoice, Distributor.name, CanonicalSku.name, CanonicalSku.category)
            .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
            .outerjoin(Distributor, Distributor.id == Invoice.distributor_id)
            .outerjoin(CanonicalSku, CanonicalSku.id == InvoiceLineItem.canonical_sku_id)
            .where(*conditions)
            .order_by(*order, Invoice.id, InvoiceLineItem.line_number)
        ).all()
        rows = [
            [
                _text(tenant.name),
                _text(inv.invoice_number),
                _date(inv.invoice_date),
                _text(distributor),
                line.line_number,
                _text(line.raw_sku),
                _text(line.raw_description),
                _text(line.raw_pack_size),
                _quantity(line.quantity),
                _text(line.uom),
                _price(line.unit_price),
                _money(line.extended_price),
                _text(product),
                _text(category),
                _quantity(line.normalized_qty_base),
                line.base_uom.value if line.base_uom else "",
                _price(line.normalized_unit_price),
                _MATCH.get(line.review_status.value, line.review_status.value),
                _STATUS.get(inv.status.value, inv.status.value),
                _invoice_link(inv),
            ]
            for line, inv, distributor, product, category in found
        ]

    audit.record(
        db,
        user,
        "invoice.exported",
        "tenant",
        tenant_id,
        tenant_id,
        kind=kind.value,
        start=start,
        end=end,
        distributor_id=distributor_id,
        rows=len(rows),
    )
    db.commit()

    period = f"{start or 'start'}-to-{end or 'now'}" if (start or end) else "all"
    filename = f"{kind.value}-{_slug(tenant.name)}-{period}.csv"
    return Response(
        _csv(header, rows),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Invoices and prices: not for a shared machine's browser cache.
            "Cache-Control": "no-store",
        },
    )
