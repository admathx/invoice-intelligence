"""A new location's first week: the same item waiting on many invoices is
matched once, confident suggestions can be accepted together, and a pack
size entered once prices the item from then on."""
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.db import bind_tenant
from app.extract.client import FAKE_PAYLOAD
from app.main import app
from app.models import Invoice, InvoiceLineItem, PriceObservation, RememberedPack
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus
from app.normalize.matcher import normalize_price
from app.packs import item_key, remember
from app.workers.tasks import process_invoice

from test_copies_and_documents import _pdf
from test_review_api import _FixedExtractor, canonical_sku, db_session, distributor, tenant  # noqa: F401


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)


@pytest.fixture()
def forget_packs(db_session, tenant):
    yield
    db_session.rollback()
    db_session.execute(delete(RememberedPack).where(RememberedPack.account_key == tenant.id))
    db_session.commit()


def _line(db, tenant, distributor, sku, *, week: int, status=ReviewStatus.pending, pack="4/5 LB", code="555001",
          description="CHEESE MOZZ SHRD", confidence="0.88", price="60.00") -> InvoiceLineItem:  # fmt: skip
    invoice = Invoice(
        id=uuid.uuid4(), tenant_id=tenant.id, distributor_id=distributor.id, invoice_number=f"W{week}-{uuid.uuid4().hex[:4]}",
        invoice_date=date(2026, 6, 1 + week), source=InvoiceSource.upload, original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )  # fmt: skip
    db.add(invoice)
    line = InvoiceLineItem(
        id=uuid.uuid4(), tenant_id=tenant.id, invoice_id=invoice.id, line_number=1, raw_description=description,
        raw_sku=code, raw_pack_size=pack, quantity=Decimal("1"), unit_price=Decimal(price),
        extended_price=Decimal(price), uom="CS", canonical_sku_id=sku.id if sku else None,
        match_confidence=Decimal(confidence) if sku else None, review_status=status,
    )  # fmt: skip
    # As the matcher leaves it: priced when the pack can be read.
    line.normalized_qty_base, line.normalized_unit_price = normalize_price(
        pack, line.quantity, line.unit_price, line.uom, sku, description
    )
    db.add(line)
    db.commit()
    return line


def _queue(tenant) -> list[dict]:
    return TestClient(app).get("/review/queue", params={"tenant_id": str(tenant.id)}).json()


def _observed(db, line_ids) -> set:
    return set(db.scalars(select(PriceObservation.invoice_line_item_id).where(PriceObservation.invoice_line_item_id.in_(line_ids))))


# --- The same item on many invoices ---------------------------------------------------


def test_an_item_waiting_on_several_invoices_is_one_card(db_session, tenant, distributor, canonical_sku):
    lines = [_line(db_session, tenant, distributor, canonical_sku, week=w) for w in range(3)]
    [item] = [i for i in _queue(tenant) if i["id"] in {str(li.id) for li in lines}]
    assert item["count"] == 3
    assert item["id"] == str(lines[-1].id)  # its latest line


def test_one_match_settles_every_invoice_the_item_is_waiting_on(db_session, tenant, distributor, canonical_sku):
    lines = [_line(db_session, tenant, distributor, canonical_sku, week=w) for w in range(3)]
    resp = TestClient(app).post(f"/review/{lines[-1].id}/confirm", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["also_settled"] == 2

    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    statuses = {db_session.get(InvoiceLineItem, li.id).review_status for li in lines}
    assert ReviewStatus.pending not in statuses
    assert _observed(db_session, [li.id for li in lines]) == {li.id for li in lines}
    assert not {str(li.id) for li in lines} & {i["id"] for i in _queue(tenant)}


def test_a_charge_marks_its_repeats_as_charges(db_session, tenant, distributor):
    lines = [_line(db_session, tenant, distributor, None, week=w, pack=None, code=None, description="PALLET CHARGE") for w in range(2)]
    resp = TestClient(app).post(f"/review/{lines[-1].id}/not-product", params={"tenant_id": str(tenant.id)})
    assert resp.json()["also_settled"] == 1
    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    assert db_session.get(InvoiceLineItem, lines[0].id).review_status == ReviewStatus.not_product


def test_accepting_suggestions_takes_only_the_confident_ones(db_session, tenant, distributor, canonical_sku):
    sure = [_line(db_session, tenant, distributor, canonical_sku, week=w, code="777", confidence="0.91") for w in range(2)]
    unsure = _line(db_session, tenant, distributor, canonical_sku, week=3, code="888", description="CHS AMER", confidence="0.70")

    resp = TestClient(app).post("/review/accept-suggestions", params={"tenant_id": str(tenant.id)}, json={"min_confidence": "0.85"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"items": 1, "lines": 2}
    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    assert all(db_session.get(InvoiceLineItem, li.id).review_status != ReviewStatus.pending for li in sure)
    assert db_session.get(InvoiceLineItem, unsure.id).review_status == ReviewStatus.pending


def test_accepting_suggestions_wont_go_below_the_suggestion_floor(tenant):
    resp = TestClient(app).post("/review/accept-suggestions", params={"tenant_id": str(tenant.id)}, json={"min_confidence": "0.3"})
    assert resp.status_code == 422


# --- A pack size entered once ------------------------------------------------------------


def test_a_pack_entered_once_prices_the_item_everywhere_it_has_none(db_session, tenant, distributor, canonical_sku, forget_packs):
    """Settled lines with no pack printed: no price, so nothing tracked."""
    lines = [
        _line(db_session, tenant, distributor, canonical_sku, week=w, status=ReviewStatus.confirmed, pack=None, code="SC1", description="SOUR CREAM")
        for w in range(3)
    ]  # fmt: skip
    assert _observed(db_session, [li.id for li in lines]) == set()

    resp = TestClient(app).post(
        f"/review/{lines[0].id}/pack", params={"tenant_id": str(tenant.id)}, json={"pack_size": "4/5 lb"}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["raw_pack_size"], body["price_known"], body["remembered"], body["applied_to"]) == ("4/5 LB", True, True, 2)
    assert Decimal(body["normalized_unit_price"]) == Decimal("3.0000")  # $60 a case of 20 lb
    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    assert _observed(db_session, [li.id for li in lines]) == {li.id for li in lines}
    assert db_session.get(InvoiceLineItem, lines[1].id).pack_size_remembered is True


def test_correcting_a_readable_printed_pack_fixes_that_line_only(db_session, tenant, distributor, canonical_sku, forget_packs):
    lines = [_line(db_session, tenant, distributor, canonical_sku, week=w, status=ReviewStatus.confirmed, pack="4/3 LB") for w in range(2)]
    resp = TestClient(app).post(f"/review/{lines[0].id}/pack", params={"tenant_id": str(tenant.id)}, json={"pack_size": "4/5 LB"})
    assert resp.json()["remembered"] is False and resp.json()["applied_to"] == 0
    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    assert db_session.get(InvoiceLineItem, lines[1].id).raw_pack_size == "4/3 LB"


def test_a_pack_that_cant_be_read_is_refused(db_session, tenant, distributor, canonical_sku):
    line = _line(db_session, tenant, distributor, canonical_sku, week=1, pack=None)
    resp = TestClient(app).post(f"/review/{line.id}/pack", params={"tenant_id": str(tenant.id)}, json={"pack_size": "a big box"})
    assert resp.status_code == 422
    assert "like 4/5 LB" in resp.json()["detail"]


def test_later_invoices_use_the_remembered_pack(db_session, tenant, monkeypatch, forget_packs):
    from app.models import Distributor

    sysco = db_session.scalar(select(Distributor).where(Distributor.slug == "sysco"))
    remember(db_session, tenant.id, sysco.id, item_key("4001122", None), "4/5 LB", None)
    db_session.commit()
    no_pack = FAKE_PAYLOAD.model_copy(
        update={"line_items": [FAKE_PAYLOAD.line_items[0].model_copy(update={"raw_pack_size": None}), FAKE_PAYLOAD.line_items[1]]}
    )
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(f"remembered {uuid.uuid4()}"), "application/pdf")}
    )
    invoice_id = resp.json()["id"]
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(no_pack))
    process_invoice(invoice_id)

    bind_tenant(db_session, tenant.id)
    line = db_session.scalar(
        select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == uuid.UUID(invoice_id), InvoiceLineItem.line_number == 1)
    )
    assert (line.raw_pack_size, line.pack_size_remembered) == ("4/5 LB", True)


def test_the_setup_checklist_counts_items_not_lines(db_session, tenant, distributor, canonical_sku):
    for w in range(3):
        _line(db_session, tenant, distributor, canonical_sku, week=w)
    _line(db_session, tenant, distributor, canonical_sku, week=4, code="999", description="BUTTER SOLID")
    setup = TestClient(app).get("/setup", params={"tenant_id": str(tenant.id)}).json()
    assert setup["waiting_to_match"] == 2
