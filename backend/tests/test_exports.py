"""CSV export of a location's invoices and line items (app/api/exports.py)."""
import csv
import io
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import AuditEvent, Invoice, InvoiceLineItem
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus

# Committed-data fixtures: the API opens its own database sessions.
from test_review_api import canonical_sku, db_session, distributor, tenant  # noqa: F401


def _invoice(db, tenant, distributor, number, when, status=InvoiceStatus.confirmed, total="100.0000") -> Invoice:
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id if distributor else None,
        invoice_number=number,
        invoice_date=when,
        subtotal=Decimal(total),
        tax=Decimal("0"),
        total=Decimal(total),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=status,
    )
    db.add(invoice)
    db.flush()
    return invoice


def _line(db, invoice, n, description, sku=None, **values) -> InvoiceLineItem:
    line = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=invoice.tenant_id,
        invoice_id=invoice.id,
        line_number=n,
        raw_description=description,
        raw_sku=values.get("raw_sku"),
        quantity=Decimal(values.get("quantity", "2.0000")),
        unit_price=Decimal(values.get("unit_price", "12.5000")),
        extended_price=Decimal(values.get("extended_price", "25.0000")),
        uom="CS",
        canonical_sku_id=sku.id if sku else None,
        normalized_unit_price=Decimal("0.5432") if sku else None,
        review_status=ReviewStatus.auto if sku else ReviewStatus.pending,
    )
    db.add(line)
    return line


def _get(tenant, kind, **params):
    resp = TestClient(app).get(f"/exports/{kind}", params={"tenant_id": str(tenant.id), **params})
    return resp


def _rows(resp) -> list[dict]:
    assert resp.status_code == 200, resp.text
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿"), "Excel needs the byte-order mark to read UTF-8"
    return list(csv.DictReader(io.StringIO(text[1:])))


def test_invoice_export_filters_by_date_and_distributor(db_session, tenant, distributor):
    _invoice(db_session, tenant, distributor, "INV-APR", date(2026, 4, 30))
    may = _invoice(db_session, tenant, distributor, "INV-MAY", date(2026, 5, 10), status=InvoiceStatus.needs_review, total="1234.5000")
    _invoice(db_session, tenant, None, "INV-MAY-OTHER", date(2026, 5, 11))
    _invoice(db_session, tenant, distributor, None, None, status=InvoiceStatus.failed)
    db_session.commit()

    everything = _rows(_get(tenant, "invoices"))
    assert len(everything) == 4, "no filter: every invoice, whatever its state"

    resp = _get(tenant, "invoices", start="2026-05-01", end="2026-05-31", distributor_id=str(distributor.id))
    (row,) = _rows(resp)
    assert row["Invoice #"] == "INV-MAY"
    assert row["Total"] == "1234.50"
    assert row["Status"] == "Needs review", "unreviewed invoices are included, and say so"
    assert row["Distributor"] == distributor.name
    assert row["Location"] == tenant.name
    assert row["Link"].endswith(f"/invoices/{may.id}?location={tenant.id}")
    assert resp.headers["content-disposition"].startswith('attachment; filename="invoices-review-api-tenant-')
    assert resp.headers["cache-control"] == "no-store"


def test_line_item_export_keeps_billed_precision_and_defuses_formulas(db_session, tenant, distributor, canonical_sku):
    invoice = _invoice(db_session, tenant, distributor, "INV-1", date(2026, 5, 1))
    _line(db_session, invoice, 1, "JALAPEÑO PEPPERS", canonical_sku, raw_sku="100", unit_price="0.5432", extended_price="1.0864")
    _line(db_session, invoice, 2, '=HYPERLINK("http://evil.example","click")', raw_sku="-5")
    # A credit for a returned case: negative, and still a number.
    _line(db_session, invoice, 3, "CREDIT RETURNED CASE", quantity="-1.0000", unit_price="12.5000", extended_price="-12.5000")
    db_session.commit()

    first, second, credit = _rows(_get(tenant, "line-items"))
    assert first["Description"] == "JALAPEÑO PEPPERS"
    assert first["Unit price"] == "0.5432"
    assert first["Extended price"] == "1.09"
    assert first["Quantity"] == "2"
    assert first["Product"] == canonical_sku.name
    assert first["Match"] == "Matched"
    assert second["Description"].startswith("'="), "a supplier's text must not run as a spreadsheet formula"
    assert second["Item code"] == "'-5"
    assert second["Match"] == "Waiting for review"
    assert credit["Quantity"] == "-1" and credit["Extended price"] == "-12.50"


def test_an_export_is_recorded_and_bad_ranges_are_refused(db_session, tenant):
    assert _get(tenant, "invoices", start="2026-06-01", end="2026-05-01").status_code == 422
    assert _get(tenant, "nonsense").status_code == 422

    _rows(_get(tenant, "line-items", start="2026-05-01"))
    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "invoice.exported")
    )
    assert event.details == {"kind": "line-items", "format": "csv", "start": "2026-05-01", "rows": 0}


def test_export_is_only_for_people_with_access(tenant):
    other_location = uuid.uuid4()
    assert TestClient(app).get("/exports/invoices", params={"tenant_id": str(other_location)}).status_code == 404


def test_the_excel_export_keeps_codes_as_printed_and_amounts_as_numbers(db_session, tenant, distributor, canonical_sku):
    """Opened from a CSV, Excel turned "0081234" into 81234 and a 16-digit
    code into 1.23457E+15. In the workbook they're text; money and dates are
    real numbers and dates."""
    from openpyxl import load_workbook

    invoice = _invoice(db_session, tenant, distributor, "0081234", date(2026, 5, 1))
    _line(db_session, invoice, 1, "MOZZ SHRD", canonical_sku, raw_sku="0012345678901234", unit_price="0.5432", extended_price="-12.5000")
    _line(db_session, invoice, 2, '=HYPERLINK("http://evil.example","click")\x07', raw_sku="-5")
    db_session.commit()

    resp = _get(tenant, "line-items", format="xlsx")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert resp.headers["content-disposition"].endswith('-all.xlsx"')
    sheet = load_workbook(io.BytesIO(resp.content)).active
    header = [c.value for c in sheet[1]]
    first = dict(zip(header, sheet[2]))
    second = dict(zip(header, sheet[3]))

    assert first["Invoice #"].value == "0081234" and first["Invoice #"].data_type == "s"
    assert first["Item code"].value == "0012345678901234"
    assert first["Extended price"].value == -12.5 and first["Extended price"].number_format == "#,##0.00"
    assert first["Unit price"].value == 0.5432
    assert first["Invoice date"].value.date() == date(2026, 5, 1)
    # Text, never a formula, and the control character a workbook can't hold is gone.
    assert second["Description"].data_type == "s"
    assert second["Description"].value == '=HYPERLINK("http://evil.example","click")'
    assert second["Item code"].value == "-5"
