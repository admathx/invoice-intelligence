"""The invoice review screen's API: fixing the numbers on an invoice that failed
the arithmetic check, and confirming it back into analytics.

Before this, an invoice in `needs_review` was a dead end. Its prices were
(rightly) kept out of analytics, but nothing could ever let them back in, so
one misread digit silently removed a real invoice from every benchmark.
"""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import SessionLocal, bind_tenant
from app.main import app
from app.models import (
    CanonicalSku,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceAlert,
    PriceObservation,
    SkuAlias,
    Tenant,
)
from app.models.enums import BaseUom, InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    """Committing session: the endpoints open their own sessions and wouldn't
    see uncommitted fixture rows. Everything created is removed in FK order."""
    session = SessionLocal()
    created: dict[str, list] = {"tenants": [], "distributors": [], "skus": []}
    session.info["_created"] = created
    try:
        yield session
    finally:
        session.rollback()
        tenants = created["tenants"]
        if tenants:
            lines = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id.in_(tenants))
            session.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(lines)))
            session.execute(delete(PriceAlert).where(PriceAlert.tenant_id.in_(tenants)))
            session.execute(delete(SkuAlias).where(SkuAlias.tenant_id.in_(tenants)))
            session.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id.in_(tenants)))
            session.execute(delete(Invoice).where(Invoice.tenant_id.in_(tenants)))
            session.execute(delete(Tenant).where(Tenant.id.in_(tenants)))
        if created["skus"]:
            session.execute(delete(CanonicalSku).where(CanonicalSku.id.in_(created["skus"])))
        if created["distributors"]:
            session.execute(delete(Distributor).where(Distributor.id.in_(created["distributors"])))
        session.commit()
        session.close()


@pytest.fixture()
def tenant(db):
    t = Tenant(name=f"Invoice Review Tenant {uuid.uuid4().hex[:8]}", metro="invoice-review-metro", volume_tier=VolumeTier.under_500k)
    db.add(t)
    db.commit()
    db.refresh(t)
    db.info["_created"]["tenants"].append(t.id)
    bind_tenant(db, t.id)
    return t


@pytest.fixture()
def distributor(db):
    d = Distributor(name="Invoice Review Distributor", slug=f"invoice-review-{uuid.uuid4().hex[:8]}")
    db.add(d)
    db.commit()
    db.refresh(d)
    db.info["_created"]["distributors"].append(d.id)
    return d


def _sku(db, name: str) -> CanonicalSku:
    sku = CanonicalSku(name=f"{name} {uuid.uuid4().hex[:8]}", category="test", base_uom=BaseUom.lb)
    db.add(sku)
    db.commit()
    db.refresh(sku)
    db.info["_created"]["skus"].append(sku.id)
    return sku


def _line(invoice, tenant, number, qty, unit, ext, pack, sku, status, normalized_price=None) -> InvoiceLineItem:
    return InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        invoice_id=invoice.id,
        line_number=number,
        raw_description=f"REVIEW TEST LINE {number}",
        raw_sku=f"RT-{number}",
        raw_pack_size=pack,
        quantity=Decimal(qty),
        unit_price=Decimal(unit),
        extended_price=Decimal(ext),
        uom="CS",
        canonical_sku_id=sku.id if sku else None,
        normalized_qty_base=Decimal(qty) * 20 if sku else None,
        normalized_unit_price=Decimal(normalized_price) if normalized_price else None,
        base_uom=BaseUom.lb if sku else None,
        review_status=status,
    )


@pytest.fixture()
def misread_invoice(db, tenant, distributor):
    """Line 1's unit price was read as $74.50 when the page says $47.50, so
    2 x 74.50 != 95.00 and the worker sent the invoice to needs_review."""
    mozzarella, tomato = _sku(db, "Mozzarella"), _sku(db, "Tomato")
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id,
        invoice_number="REVIEW-0001",
        invoice_date=date(2026, 5, 1),
        subtotal=Decimal("152.50"),
        tax=Decimal("0.00"),
        total=Decimal("152.50"),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.needs_review,
    )
    db.add(invoice)
    lines = [
        _line(invoice, tenant, 1, "2", "74.50", "95.00", "4/5 LB", mozzarella, ReviewStatus.auto, "3.7250"),
        _line(invoice, tenant, 2, "1", "47.50", "47.50", "4/5 LB", tomato, ReviewStatus.auto, "2.3750"),
        _line(invoice, tenant, 3, "1", "10.00", "10.00", "4/5 LB", None, ReviewStatus.pending),
    ]
    db.add_all(lines)
    db.commit()
    return invoice, lines


def _url(invoice, tenant, suffix=""):
    return f"/invoices/{invoice.id}{suffix}?tenant_id={tenant.id}"


def _observations(db, lines) -> dict[uuid.UUID, PriceObservation]:
    db.expire_all()
    rows = db.scalars(select(PriceObservation).where(PriceObservation.invoice_line_item_id.in_([l.id for l in lines])))
    return {row.invoice_line_item_id: row for row in rows}


# --- the check the screen shows ---------------------------------------------


def test_the_detail_explains_what_doesnt_add_up(client, misread_invoice, tenant):
    invoice, _ = misread_invoice

    body = client.get(_url(invoice, tenant)).json()

    assert body["check"]["passes"] is False
    assert body["check"]["failed_line_numbers"] == [1]
    assert any("line(s) [1]" in reason for reason in body["check"]["reasons"])


def test_confirming_is_refused_while_the_numbers_still_dont_add_up(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.post(_url(invoice, tenant, "/confirm"))

    assert resp.status_code == 422
    assert resp.json()["detail"]["reasons"]
    assert _observations(db, lines) == {}


# --- fixing and confirming --------------------------------------------------


def test_correcting_the_misread_price_reprices_the_line_and_passes_the_check(client, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["check"]["passes"] is True
    fixed = next(li for li in body["line_items"] if li["line_number"] == 1)
    # Re-derived from the corrected number, not left at the misread one:
    # $47.50 per 20 lb case is $2.3750/lb (it was $3.7250 from $74.50).
    assert Decimal(fixed["normalized_unit_price"]) == Decimal("2.3750")
    # Editing doesn't confirm: the person decides when they're done.
    assert body["status"] == InvoiceStatus.needs_review.value


def test_confirming_lets_the_invoice_back_into_analytics(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    resp = client.post(_url(invoice, tenant, "/confirm"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == InvoiceStatus.confirmed.value
    observations = _observations(db, lines)
    # Both settled lines, at the corrected price...
    assert set(observations) == {lines[0].id, lines[1].id}
    assert observations[lines[0].id].unit_price_base == Decimal("2.3750")
    # ...but not the line whose SKU is still unknown.
    assert lines[2].id not in observations


def test_a_line_resolved_later_on_a_confirmed_invoice_still_reaches_analytics(client, db, misread_invoice, tenant):
    """The pending line waits for the line queue; when that resolves it, the
    invoice is `confirmed` rather than `extracted`, and must still count."""
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})
    client.post(_url(invoice, tenant, "/confirm"))

    resp = client.post(
        f"/review/{lines[2].id}/correct",
        params={"tenant_id": str(tenant.id)},
        json={"canonical_sku_id": str(lines[1].canonical_sku_id)},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["wrote_price_observation"] is True
    assert lines[2].id in _observations(db, lines)


def test_an_invoice_can_only_be_confirmed_once(client, misread_invoice, tenant):
    invoice, lines = misread_invoice
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})
    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 200

    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 409


# --- what can't be edited ---------------------------------------------------


def test_an_extracted_invoice_cant_be_edited(client, db, misread_invoice, tenant):
    """Its numbers already feed analytics; rewriting them in place would
    silently move benchmarks other tenants are reading."""
    invoice, lines = misread_invoice
    db.get(Invoice, invoice.id).status = InvoiceStatus.extracted
    db.commit()

    resp = client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "1.00"}]})

    assert resp.status_code == 409


def test_a_line_from_another_invoice_is_rejected_and_nothing_changes(client, db, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(
        _url(invoice, tenant),
        json={
            "subtotal": "1.00",
            "line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}, {"id": str(uuid.uuid4()), "unit_price": "1"}],
        },
    )

    assert resp.status_code == 422
    db.expire_all()
    assert db.get(Invoice, invoice.id).subtotal == Decimal("152.50")
    assert db.get(InvoiceLineItem, lines[0].id).unit_price == Decimal("74.50")


def test_a_value_too_large_for_the_column_is_a_422_not_a_database_error(client, misread_invoice, tenant):
    invoice, lines = misread_invoice

    resp = client.patch(
        _url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "123456789012.5"}]}
    )

    assert resp.status_code == 422


def test_an_unrecognized_distributor_blocks_confirmation_until_one_is_chosen(client, db, misread_invoice, tenant, distributor):
    invoice, lines = misread_invoice
    db.get(Invoice, invoice.id).distributor_id = None
    db.commit()

    reasons = client.get(_url(invoice, tenant)).json()["check"]["reasons"]
    assert any("distributor" in reason for reason in reasons)

    resp = client.patch(
        _url(invoice, tenant),
        json={"distributor_id": str(distributor.id), "line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["distributor_name"] == distributor.name
    assert not any("distributor" in reason for reason in resp.json()["check"]["reasons"])


def test_extractions_other_counts_as_unrecognized_not_as_a_distributor(client, db, misread_invoice, tenant):
    """The worker stores extraction's 'other' as a real distributor row, so a
    NULL check alone let an invoice nobody could attribute be confirmed."""
    invoice, lines = misread_invoice
    other = db.scalar(select(Distributor).where(Distributor.slug == "other"))
    assert other is not None, "distributors not seeded — run `make seed`"
    db.get(Invoice, invoice.id).distributor_id = other.id
    db.commit()
    client.patch(_url(invoice, tenant), json={"line_items": [{"id": str(lines[0].id), "unit_price": "47.50"}]})

    check = client.get(_url(invoice, tenant)).json()["check"]
    assert any("distributor" in reason for reason in check["reasons"])
    assert client.post(_url(invoice, tenant, "/confirm")).status_code == 422
    # And it isn't offered as a choice.
    assert "other" not in {d["slug"] for d in client.get("/distributors").json()}


def test_other_cant_be_chosen_as_the_correction(client, db, misread_invoice, tenant):
    invoice, _ = misread_invoice
    other = db.scalar(select(Distributor).where(Distributor.slug == "other"))

    assert client.patch(_url(invoice, tenant), json={"distributor_id": str(other.id)}).status_code == 422


# --- entering lines by hand --------------------------------------------------


def _invoice(db, tenant, distributor, status, *, number, day, subtotal=None, total=None) -> Invoice:
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        distributor_id=distributor.id if distributor else None,
        invoice_number=number,
        invoice_date=date(2026, 4, day),
        subtotal=Decimal(subtotal) if subtotal else None,
        tax=Decimal("0.00") if subtotal else None,
        total=Decimal(total) if total else None,
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=status,
    )
    db.add(invoice)
    db.commit()
    return invoice


def _bought(db, tenant, invoice, *, sku_code, unit, sku=None) -> None:
    db.add(
        InvoiceLineItem(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            invoice_id=invoice.id,
            line_number=1,
            raw_description=f"MOZZ SHRD WHL MLK {sku_code}",
            raw_sku=sku_code,
            raw_pack_size="4/5 LB",
            quantity=Decimal("1"),
            unit_price=Decimal(unit),
            extended_price=Decimal(unit),
            uom="CS",
            canonical_sku_id=sku.id if sku else None,
            review_status=ReviewStatus.auto,
        )
    )
    db.commit()


@pytest.fixture()
def empty_invoice(db, tenant, distributor):
    """Extraction found no line items: the worker sends this to needs_review
    with 'no line items', and until now nothing could add the first one."""
    return _invoice(db, tenant, distributor, InvoiceStatus.needs_review, number="EMPTY-1", day=20)


def _new_line(**overrides):
    body = {
        "raw_description": "MOZZ SHRD WHL MLK MOZ-1",
        "raw_sku": "MOZ-1",
        "raw_pack_size": "4/5 LB",
        "uom": "cs",
        "quantity": "2",
        "unit_price": "47.50",
        "extended_price": "95.00",
    }
    body.update(overrides)
    return body


def test_the_first_line_can_be_added_to_an_invoice_extraction_found_empty(client, db, empty_invoice, tenant, distributor):
    sku = _sku(db, "Mozzarella")
    # This tenant corrected MOZ-1 before, so the hand-entered line should
    # resolve the way that purchase did, through the tenant's own alias.
    db.add(SkuAlias(tenant_id=tenant.id, canonical_sku_id=sku.id, distributor_id=distributor.id,
                    raw_description="MOZZ SHRD WHL MLK MOZ-1", raw_sku="MOZ-1"))
    db.commit()

    resp = client.post(_url(empty_invoice, tenant, "/line-items"), json=_new_line())

    assert resp.status_code == 201, resp.text
    [line] = resp.json()["line_items"]
    assert line["line_number"] == 1
    assert line["uom"] == "CS"
    assert line["canonical_sku_id"] == str(sku.id)
    assert Decimal(line["normalized_unit_price"]) == Decimal("2.3750")  # $47.50 per 20 lb case
    assert line["extraction_confidence"] is None  # typed, not extracted
    # Now the only thing missing is the totals the page prints.
    assert not any("no line items" in r for r in resp.json()["check"]["reasons"])


def test_hand_entered_lines_are_checked_like_extracted_ones(client, empty_invoice, tenant):
    """Every printed number is typed in and nothing is derived, so a typo in
    the extended price is caught rather than agreed with."""
    resp = client.post(_url(empty_invoice, tenant, "/line-items"), json=_new_line(extended_price="59.00"))

    assert resp.json()["check"]["failed_line_numbers"] == [1]


def test_a_failed_invoice_recovered_by_hand_can_be_confirmed(client, db, tenant, distributor):
    invoice = _invoice(db, tenant, distributor, InvoiceStatus.failed, number="FAILED-1", day=21)

    added = client.post(_url(invoice, tenant, "/line-items"), json=_new_line())
    # Editing takes it under review, which also stops a retry of the
    # extraction job from clearing the lines just typed in.
    assert added.json()["status"] == InvoiceStatus.needs_review.value

    client.patch(_url(invoice, tenant), json={"subtotal": "95.00", "tax": "0.00", "total": "95.00"})
    resp = client.post(_url(invoice, tenant, "/confirm"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == InvoiceStatus.confirmed.value


def test_a_worker_retry_leaves_hand_entered_lines_alone(client, db, tenant, distributor):
    from app.workers.tasks import process_invoice

    invoice = _invoice(db, tenant, distributor, InvoiceStatus.failed, number="FAILED-2", day=22)
    client.post(_url(invoice, tenant, "/line-items"), json=_new_line())

    process_invoice(str(invoice.id))  # e.g. RQ retrying the original job

    db.expire_all()
    assert len(db.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice.id)).all()) == 1


def test_lines_cant_be_added_to_an_invoice_that_already_feeds_analytics(client, db, tenant, distributor):
    invoice = _invoice(db, tenant, distributor, InvoiceStatus.extracted, number="DONE-1", day=23)

    assert client.post(_url(invoice, tenant, "/line-items"), json=_new_line()).status_code == 409


def test_a_hand_entered_line_needs_its_printed_numbers(client, empty_invoice, tenant):
    body = _new_line()
    del body["extended_price"]
    assert client.post(_url(empty_invoice, tenant, "/line-items"), json=body).status_code == 422
    assert client.post(_url(empty_invoice, tenant, "/line-items"), json=_new_line(raw_description="")).status_code == 422


def test_a_mistaken_line_can_be_removed(client, empty_invoice, tenant):
    line_id = client.post(_url(empty_invoice, tenant, "/line-items"), json=_new_line()).json()["line_items"][0]["id"]

    resp = client.delete(_url(empty_invoice, tenant, f"/line-items/{line_id}"))

    assert resp.status_code == 200, resp.text
    assert resp.json()["line_items"] == []
    assert client.delete(_url(empty_invoice, tenant, f"/line-items/{line_id}")).status_code == 404


# --- suggestions from past invoices -----------------------------------------


def test_suggestions_offer_the_tenants_past_purchases_with_the_last_trusted_price(
    client, db, empty_invoice, tenant, distributor
):
    older = _invoice(db, tenant, distributor, InvoiceStatus.extracted, number="PAST-1", day=1)
    _bought(db, tenant, older, sku_code="MOZ-1", unit="45.00")
    newer = _invoice(db, tenant, distributor, InvoiceStatus.confirmed, number="PAST-2", day=8)
    _bought(db, tenant, newer, sku_code="MOZ-1", unit="47.50")
    # A later purchase whose numbers never passed the check: its price must
    # not become the hint, because it may be the misread one.
    misread = _invoice(db, tenant, distributor, InvoiceStatus.needs_review, number="PAST-3", day=15)
    _bought(db, tenant, misread, sku_code="MOZ-1", unit="74.50")

    [suggestion] = client.get(_url(empty_invoice, tenant, "/line-item-suggestions"), params={"q": "moz"}).json()

    assert suggestion["raw_sku"] == "MOZ-1"
    assert suggestion["raw_pack_size"] == "4/5 LB"
    assert Decimal(suggestion["last_unit_price"]) == Decimal("47.50")
    assert suggestion["last_seen"] == "2026-04-08"


def test_suggestions_never_include_another_businesss_purchases(client, db, empty_invoice, tenant, distributor):
    other = Tenant(name=f"Other Business {uuid.uuid4().hex[:8]}", metro="invoice-review-metro", volume_tier=VolumeTier.under_500k)
    db.add(other)
    db.commit()
    db.info["_created"]["tenants"].append(other.id)
    bind_tenant(db, other.id)
    theirs = _invoice(db, other, distributor, InvoiceStatus.extracted, number="THEIRS-1", day=2)
    _bought(db, other, theirs, sku_code="SECRET-9", unit="1.00")
    bind_tenant(db, tenant.id)

    codes = {s["raw_sku"] for s in client.get(_url(empty_invoice, tenant, "/line-item-suggestions")).json()}

    assert "SECRET-9" not in codes
