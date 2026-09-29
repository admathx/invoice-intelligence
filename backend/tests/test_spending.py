"""Monthly spending by category and distributor (app/api/spending.py), and
the first-time checklist (app/api/setup.py)."""
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.main import app
from app.models import Invoice, InvoiceLineItem
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus

from test_review_api import canonical_sku, db_session, distributor, tenant  # noqa: F401


def _invoice(db, tenant, distributor, when, lines, status=InvoiceStatus.confirmed):
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id if distributor else None,
        invoice_date=when,
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=status,
    )
    db.add(invoice)
    db.flush()
    for n, (amount, sku) in enumerate(lines, start=1):
        db.add(
            InvoiceLineItem(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                invoice_id=invoice.id,
                line_number=n,
                raw_description=f"LINE {n}",
                quantity=Decimal("1"),
                unit_price=Decimal(amount),
                extended_price=Decimal(amount),
                uom="CS",
                canonical_sku_id=sku.id if sku else None,
                review_status=ReviewStatus.auto if sku else ReviewStatus.pending,
            )
        )
    db.commit()


def _get(tenant, today="2026-09-15"):
    resp = TestClient(app).get("/spending", params={"tenant_id": str(tenant.id), "today": today})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_months_add_up_by_category_and_distributor(db_session, tenant, distributor, canonical_sku):
    _invoice(db_session, tenant, distributor, date(2026, 7, 3), [("100.00", canonical_sku), ("20.00", None)])
    _invoice(db_session, tenant, distributor, date(2026, 8, 30), [("250.50", canonical_sku)])
    # Not counted: needs a look, and one without a date.
    _invoice(db_session, tenant, distributor, date(2026, 8, 1), [("999.00", canonical_sku)], status=InvoiceStatus.needs_review)
    _invoice(db_session, tenant, distributor, None, [("5.00", canonical_sku)])

    body = _get(tenant)
    assert [m["month"] for m in body["months"]] == ["2026-07", "2026-08", "2026-09"], "first month with spending to now"
    july, august, september = body["months"]
    assert july["total"] == "120.00" and july["invoice_count"] == 1
    assert july["by_category"] == {"Test": "100.00", "Not matched yet": "20.00"}
    assert july["by_distributor"] == {distributor.name: "120.00"}
    assert august["total"] == "250.50"
    assert september == {"month": "2026-09", "total": "0.00", "invoice_count": 0, "by_category": {}, "by_distributor": {}}
    assert body["not_counted"] == 1


def test_at_most_a_year_and_empty_months_stay_in(db_session, tenant, distributor, canonical_sku):
    _invoice(db_session, tenant, distributor, date(2025, 9, 30), [("10.00", canonical_sku)])  # over a year ago
    _invoice(db_session, tenant, distributor, date(2025, 10, 1), [("10.00", canonical_sku)])
    months = _get(tenant)["months"]
    assert months[0]["month"] == "2025-10" and months[-1]["month"] == "2026-09"
    assert len(months) == 12
    assert [m["total"] for m in months].count("0.00") == 11


def test_a_location_with_nothing_yet_shows_just_this_month(tenant):
    assert _get(tenant)["months"] == [
        {"month": "2026-09", "total": "0.00", "invoice_count": 0, "by_category": {}, "by_distributor": {}}
    ]


# --- the first-time checklist (app/api/setup.py) ---------------------------------


def test_setup_steps_tick_themselves_off(db_session, tenant, distributor, canonical_sku):
    from app.models import TenantMembership, User

    client = TestClient(app)
    url = f"/setup?tenant_id={tenant.id}"
    assert client.get(url).json() == {
        "first_invoice": False,
        "emailed_invoice": False,
        "items_matched": False,
        "team_added": False,
        "inbox_address": tenant.inbox_address,
        "waiting_to_match": 0,
    }

    _invoice(db_session, tenant, distributor, date(2026, 8, 1), [("10.00", None)])  # one item left to match
    steps = client.get(url).json()
    assert steps["first_invoice"] and not steps["items_matched"] and steps["waiting_to_match"] == 1

    invoice = db_session.scalar(select(Invoice).where(Invoice.tenant_id == tenant.id))
    invoice.source = InvoiceSource.email
    db_session.execute(
        update(InvoiceLineItem)
        .where(InvoiceLineItem.invoice_id == invoice.id)
        .values(review_status=ReviewStatus.confirmed)
    )
    users = [User(id=uuid.uuid4(), email=f"setup-{uuid.uuid4().hex[:6]}@test.invalid", name="S", password_hash="!") for _ in range(2)]
    db_session.add_all(users)
    db_session.flush()
    db_session.add_all([TenantMembership(user_id=u.id, tenant_id=tenant.id) for u in users])
    db_session.commit()
    try:
        steps = client.get(url).json()
        assert steps["emailed_invoice"] and steps["items_matched"] and steps["team_added"]
    finally:
        for u in users:
            db_session.delete(u)
        db_session.commit()
