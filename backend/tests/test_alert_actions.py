"""Dismissing a price alert, and what screens need to fix a wrong match."""
import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.models import AuditEvent, Distributor, PriceAlert, PriceObservation
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


def test_an_alert_card_says_where_the_product_costs_less(db_session, tenant, distributor, canonical_sku):
    """The alert is about one distributor's price. The same product has been
    coming from another for less: the card says so (app/analytics/alternatives.py)."""
    elsewhere = Distributor(name="Elsewhere Foods", slug=f"review-api-{uuid.uuid4().hex[:8]}")
    db_session.add(elsewhere)
    db_session.commit()
    db_session.info["_created"]["distributors"].append(elsewhere.id)
    line = _auto_matched_line(db_session, tenant, elsewhere, canonical_sku)  # $7.00, on 2026-05-01
    db_session.add_all(
        [
            PriceObservation(
                tenant_id=tenant.id,
                canonical_sku_id=canonical_sku.id,
                distributor_id=elsewhere.id,
                observed_on=date(2026, 7, 20),
                unit_price_base=Decimal("8.50"),
                metro=tenant.metro,
                volume_tier=tenant.volume_tier,
                invoice_line_item_id=line.id,
            ),
            PriceAlert(
                tenant_id=tenant.id,
                canonical_sku_id=canonical_sku.id,
                distributor_id=distributor.id,
                alert_type=AlertType.creep,
                baseline_price=Decimal("9"),
                current_price=Decimal("10"),
                pct_change=Decimal("0.1111"),
                window_start=date(2026, 5, 1),
                window_end=date(2026, 8, 1),
                status=AlertStatus.open,
            ),
        ]
    )
    db_session.commit()

    [card] = TestClient(app).get(f"/insights?tenant_id={tenant.id}").json()

    [offer] = card["alternatives"]
    assert offer["distributor_name"] == "Elsewhere Foods"
    assert (Decimal(offer["price"]), Decimal(offer["saving_pct"])) == (Decimal("8.50"), Decimal("0.15"))
    assert (offer["yours"], offer["last_bought"], offer["distinct_account_count"]) == (True, "2026-07-20", None)


def test_screens_get_what_they_need_to_fix_a_wrong_match(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    client = TestClient(app)
    detail = client.get(f"/invoices/{line.invoice_id}?tenant_id={tenant.id}").json()
    assert detail["line_items"][0]["canonical_sku_name"] == canonical_sku.name
    history = client.get(f"/skus/{canonical_sku.id}?tenant_id={tenant.id}").json()
    assert history["matched_lines"][0]["line_id"] == str(line.id)

    reopened = client.post(f"/review/{line.id}/reopen?tenant_id={tenant.id}")
    assert reopened.status_code == 200 and reopened.json()["review_status"] == "pending"
