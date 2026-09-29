"""Dismissing a price alert, and what screens need to fix a wrong match."""
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import AuditEvent, PriceAlert
from app.models.enums import AlertStatus, AlertType

from test_review_api import (  # noqa: F401  (committed-data fixtures)
    _auto_matched_line,
    canonical_sku,
    db_session,
    distributor,
    tenant,
)


def test_a_dismissed_alert_leaves_price_alerts(db_session, tenant, canonical_sku):
    alert = PriceAlert(
        tenant_id=tenant.id,
        canonical_sku_id=canonical_sku.id,
        alert_type=AlertType.creep,
        baseline_price=Decimal("10"),
        current_price=Decimal("12"),
        pct_change=Decimal("0.2"),
        window_start=date(2026, 5, 1),
        window_end=date(2026, 8, 1),
        status=AlertStatus.open,
    )
    db_session.add(alert)
    db_session.commit()
    client = TestClient(app)
    url = f"/insights/{alert.id}/dismiss?tenant_id={tenant.id}"

    assert client.post(url).status_code == 204
    db_session.expire_all()
    assert db_session.get(PriceAlert, alert.id).status == AlertStatus.dismissed
    assert client.get(f"/insights?tenant_id={tenant.id}").json() == []
    assert db_session.scalar(select(AuditEvent.action).where(AuditEvent.entity_id == alert.id)) == "price_alert.dismissed"

    assert client.post(url).status_code == 404, "already dismissed"
    assert client.post(f"/insights/{uuid.uuid4()}/dismiss?tenant_id={tenant.id}").status_code == 404


def test_screens_get_what_they_need_to_fix_a_wrong_match(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    client = TestClient(app)
    detail = client.get(f"/invoices/{line.invoice_id}?tenant_id={tenant.id}").json()
    assert detail["line_items"][0]["canonical_sku_name"] == canonical_sku.name
    history = client.get(f"/skus/{canonical_sku.id}?tenant_id={tenant.id}").json()
    assert history["matched_lines"][0]["line_id"] == str(line.id)

    reopened = client.post(f"/review/{line.id}/reopen?tenant_id={tenant.id}")
    assert reopened.status_code == 200 and reopened.json()["review_status"] == "pending"
