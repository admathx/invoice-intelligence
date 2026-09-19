"""Covers upsert_creep_alerts, which had zero call sites anywhere in the repo
per code review on Phase 4 — specifically the dedup-by-open-alert-per-
canonical_sku_id logic that assumes two open creep alerts never coexist for
the same SKU.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.analytics.price_creep import RECENT_WINDOW_SIZE, upsert_creep_alerts
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation, Tenant
from app.models.enums import AlertStatus, AlertType, BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier

AS_OF = date(2026, 6, 1)


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Creep Alert Test Distributor", slug=f"creep-alert-test-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


@pytest.fixture()
def tenant(db_session):
    t = Tenant(name=f"Creep Alert Test Tenant {uuid.uuid4().hex[:8]}", metro="creep-test-metro", volume_tier=VolumeTier.under_500k)
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture()
def canonical_sku(db_session):
    sku = CanonicalSku(name=f"Creep Alert Test SKU {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


def _add_observation(db, tenant, sku, distributor, observed_on: date, price: str) -> None:
    invoice_id = uuid.uuid4()
    db.add(
        Invoice(
            id=invoice_id,
            tenant_id=tenant.id,
            distributor_id=distributor.id,
            invoice_date=observed_on,
            source=InvoiceSource.upload,
            original_file_uri="file:///dev/null",
            status=InvoiceStatus.extracted,
        )
    )
    line_item_id = uuid.uuid4()
    db.add(
        InvoiceLineItem(
            id=line_item_id,
            tenant_id=tenant.id,
            invoice_id=invoice_id,
            line_number=1,
            raw_description="Creep Alert Test Line",
            quantity=Decimal("1"),
            unit_price=Decimal(price),
            extended_price=Decimal(price),
            uom="LB",
            canonical_sku_id=sku.id,
            normalized_qty_base=Decimal("1"),
            normalized_unit_price=Decimal(price),
            base_uom=sku.base_uom,
            review_status=ReviewStatus.auto,
        )
    )
    db.add(
        PriceObservation(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            canonical_sku_id=sku.id,
            distributor_id=distributor.id,
            observed_on=observed_on,
            unit_price_base=Decimal(price),
            metro=tenant.metro,
            volume_tier=tenant.volume_tier,
            invoice_line_item_id=line_item_id,
        )
    )
    db.commit()


def _seed_creep(db, tenant, sku, distributor) -> None:
    for i in range(8):
        _add_observation(db, tenant, sku, distributor, AS_OF - timedelta(weeks=13 - i), "10.00")
    for i in range(RECENT_WINDOW_SIZE):
        _add_observation(db, tenant, sku, distributor, AS_OF - timedelta(weeks=RECENT_WINDOW_SIZE - 1 - i), "15.00")


def test_upsert_creates_one_open_alert(db_session, tenant, canonical_sku, distributor):
    _seed_creep(db_session, tenant, canonical_sku, distributor)

    alerts = upsert_creep_alerts(db_session, tenant.id)

    assert len(alerts) == 1
    assert alerts[0].canonical_sku_id == canonical_sku.id
    assert alerts[0].alert_type == AlertType.creep
    assert alerts[0].status == AlertStatus.open
    assert alerts[0].current_price == Decimal("15.0000")


def test_rerun_updates_the_existing_alert_instead_of_duplicating(db_session, tenant, canonical_sku, distributor):
    _seed_creep(db_session, tenant, canonical_sku, distributor)
    first_run = upsert_creep_alerts(db_session, tenant.id)
    assert len(first_run) == 1
    first_alert_id = first_run[0].id

    # A further price rise, then rerun — must update the same open alert row,
    # never create a second open creep alert for the same canonical_sku_id.
    _add_observation(db_session, tenant, canonical_sku, distributor, AS_OF + timedelta(days=1), "20.00")
    second_run = upsert_creep_alerts(db_session, tenant.id)

    assert len(second_run) == 1
    assert second_run[0].id == first_alert_id

    open_alerts = list(
        db_session.scalars(
            select(PriceAlert).where(
                PriceAlert.tenant_id == tenant.id,
                PriceAlert.canonical_sku_id == canonical_sku.id,
                PriceAlert.alert_type == AlertType.creep,
                PriceAlert.status == AlertStatus.open,
            )
        )
    )
    assert len(open_alerts) == 1
