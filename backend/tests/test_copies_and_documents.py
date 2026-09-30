"""What a realistic test set showed the app getting wrong about what arrives:
the same invoice counted several times, statements and price lists read as
invoices, local vendors that could never be attributed, fees waiting on
Match items, and free or returned items recorded as prices.
"""
import io
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import delete, select, update

from app.db import bind_tenant
from app.duplicates import number_key
from app.extract.charges import is_charge
from app.extract.client import FAKE_PAYLOAD
from app.extract.schema import ExtractedLineItem
from app.ingest.upload import InvalidInvoiceFileError, validate_invoice_bytes
from app.main import app
from app.models import Distributor, Invoice, InvoiceLineItem, PriceObservation
from app.models.enums import InvoiceStatus, ReviewStatus
from app.workers.tasks import process_invoice

# The committing fixtures (the API and the worker open their own sessions).
from test_review_api import (  # noqa: F401
    _FixedExtractor,
    _auto_matched_line,
    _line_ids,
    _make_test_pdf,
    canonical_sku,
    db_session,
    distributor,
    tenant,
)


@pytest.fixture(autouse=True)
def _no_queue(monkeypatch):
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda *a, **k: None)


@pytest.fixture()
def own_distributors(db_session, tenant):
    """Distributors this test's location adds, removed afterwards."""
    yield
    db_session.rollback()
    theirs = select(Distributor.id).where(Distributor.account_key == tenant.id)
    bind_tenant(db_session, tenant.id)
    db_session.execute(update(Invoice).where(Invoice.distributor_id.in_(theirs)).values(distributor_id=None))
    db_session.execute(delete(Distributor).where(Distributor.account_key == tenant.id))
    db_session.commit()


def _pdf(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buf.getvalue()


def _post(tenant, data: bytes):
    return TestClient(app).post(f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", data, "application/pdf")})


def _read(monkeypatch, tenant, data: bytes, payload) -> Invoice:
    resp = _post(tenant, data)
    assert resp.status_code == 201, resp.text
    invoice_id = uuid.UUID(resp.json()["id"])
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(payload))
    process_invoice(str(invoice_id))
    return invoice_id


def _detail(tenant, invoice_id) -> dict:
    return TestClient(app).get(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)}).json()


def _queue(tenant) -> set[str]:
    return {i["id"] for i in TestClient(app).get("/review/queue", params={"tenant_id": str(tenant.id)}).json()}


# --- Copies --------------------------------------------------------------------


def test_the_same_file_twice_is_refused_before_it_is_stored(db_session, tenant):
    data = _pdf("Invoice 88214")
    assert _post(tenant, data).status_code == 201
    again = _post(tenant, data)
    assert again.status_code == 409
    assert "already added this file" in again.json()["detail"]
    assert len(db_session.scalars(select(Invoice.id).where(Invoice.tenant_id == tenant.id)).all()) == 1


def test_a_rescan_of_an_invoice_is_held_as_a_likely_copy(db_session, tenant, monkeypatch):
    """The test set's G-10: the PDF, a copy and a phone rescan all counted."""
    first = _read(monkeypatch, tenant, _pdf("original"), FAKE_PAYLOAD)
    rescan = FAKE_PAYLOAD.model_copy(update={"invoice_number": " fake-0001 "})
    copy = _read(monkeypatch, tenant, _pdf("rescan"), rescan)

    db_session.expire_all()
    assert db_session.get(Invoice, first).duplicate_of_id is None
    held = db_session.get(Invoice, copy)
    assert held.duplicate_of_id == first and held.status == InvoiceStatus.needs_review
    detail = _detail(tenant, copy)
    assert detail["check"]["passes"] is False
    assert any("copy of invoice FAKE-0001" in r for r in detail["check"]["reasons"])
    # Nothing on it is used or waiting to be matched.
    assert not {str(i) for i in _line_ids(db_session, copy)} & _queue(tenant)
    assert db_session.scalars(
        select(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(_line_ids(db_session, copy)))
    ).all() == []


def test_a_held_copy_can_be_kept_as_a_different_invoice(db_session, tenant, monkeypatch):
    _read(monkeypatch, tenant, _pdf("one"), FAKE_PAYLOAD)
    copy = _read(monkeypatch, tenant, _pdf("two"), FAKE_PAYLOAD)
    resp = TestClient(app).post(f"/invoices/{copy}/keep", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["duplicate_of_id"] is None
    assert not any("copy" in r for r in resp.json()["check"]["reasons"])
    # Its lines are matched now, like any invoice's.
    assert {str(i) for i in _line_ids(db_session, copy)} <= _queue(tenant) | {
        str(li.id) for li in db_session.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == copy))
        if li.review_status != ReviewStatus.pending
    }


def test_deleting_an_invoice_takes_its_prices_with_it(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    observation = PriceObservation(
        id=uuid.uuid4(), tenant_id=tenant.id, canonical_sku_id=canonical_sku.id, distributor_id=distributor.id,
        observed_on=__import__("datetime").date(2026, 5, 1), unit_price_base=Decimal("7"), metro=tenant.metro,
        volume_tier=tenant.volume_tier, invoice_line_item_id=line.id,
    )  # fmt: skip
    db_session.add(observation)
    db_session.commit()
    invoice_id, observation_id = line.invoice_id, observation.id

    resp = TestClient(app).delete(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)})

    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id) is None
    assert db_session.get(PriceObservation, observation_id) is None


def test_invoice_numbers_compare_as_a_rescan_reads_them():
    assert number_key("# 0088214") == number_key("88214") == "88214"
    assert number_key("inv-12a") == "INV12A"
    assert number_key(None) == ""


# --- Not invoices ----------------------------------------------------------------


def test_a_statement_is_held_and_nothing_on_it_is_used(db_session, tenant, monkeypatch):
    statement = FAKE_PAYLOAD.model_copy(update={"document_type": "statement", "invoice_number": "STMT-9"})
    invoice_id = _read(monkeypatch, tenant, _pdf("statement"), statement)

    detail = _detail(tenant, invoice_id)
    assert detail["document_type"] == "statement"
    assert any("a statement, not an invoice" in r for r in detail["check"]["reasons"])
    assert not {str(i) for i in _line_ids(db_session, invoice_id)} & _queue(tenant)
    assert TestClient(app).post(f"/invoices/{invoice_id}/confirm", params={"tenant_id": str(tenant.id)}).status_code == 422


def test_a_credit_memo_is_read_like_an_invoice(db_session, tenant, monkeypatch):
    memo = FAKE_PAYLOAD.model_copy(update={"document_type": "credit_memo", "invoice_number": "CM-1"})
    invoice_id = _read(monkeypatch, tenant, _pdf("credit"), memo)
    assert _detail(tenant, invoice_id)["document_type"] is None


def test_a_password_protected_pdf_is_refused_with_the_reason():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, encrypt="invoice123")
    c.drawString(72, 720, "locked")
    c.showPage()
    c.save()
    with pytest.raises(InvalidInvoiceFileError, match="password-protected"):
        validate_invoice_bytes(buf.getvalue())


# --- A business's own distributors ----------------------------------------------------


def test_a_location_adds_a_local_distributor_only_its_business_sees(db_session, tenant, own_distributors):
    client = TestClient(app)
    added = client.post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "FreshLine Produce Co."})
    assert added.status_code == 200, added.text
    again = client.post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "Freshline Produce Company"})
    assert again.json()["id"] == added.json()["id"]
    shared = client.post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "SYSCO"})
    assert shared.json()["slug"] == "sysco"

    names = {d["name"] for d in client.get("/distributors", params={"tenant_id": str(tenant.id)}).json()}
    assert "FreshLine Produce Co." in names and "Other" not in names
    other_location = db_session.scalar(select(Invoice.tenant_id).where(Invoice.tenant_id != tenant.id).limit(1))
    if other_location is not None:
        theirs = client.get("/distributors", params={"tenant_id": str(other_location)})
        assert "FreshLine Produce Co." not in {d["name"] for d in theirs.json()}


def test_the_next_invoice_from_a_local_distributor_is_recognized_by_name(
    db_session, tenant, monkeypatch, own_distributors
):
    TestClient(app).post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "Gulf Seafood Supply"})
    from_them = FAKE_PAYLOAD.model_copy(update={"distributor": "other", "distributor_name": "GULF SEAFOOD SUPPLY, INC."})
    invoice_id = _read(monkeypatch, tenant, _pdf("gulf"), from_them)

    detail = _detail(tenant, invoice_id)
    assert detail["distributor_name"] == "Gulf Seafood Supply"
    assert detail["printed_distributor"] == "GULF SEAFOOD SUPPLY, INC."
    assert not any("distributor" in r for r in detail["check"]["reasons"])


def test_another_business_s_distributor_cant_be_chosen(db_session, tenant, distributor, canonical_sku):
    theirs = Distributor(name="Someone Else's Vendor", slug=f"theirs-{uuid.uuid4().hex[:8]}", account_key=uuid.uuid4())
    db_session.add(theirs)
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    invoice = db_session.get(Invoice, line.invoice_id)
    invoice.status = InvoiceStatus.needs_review
    db_session.commit()
    try:
        resp = TestClient(app).patch(
            f"/invoices/{invoice.id}", params={"tenant_id": str(tenant.id)}, json={"distributor_id": str(theirs.id)}
        )
        assert resp.status_code == 422
    finally:
        db_session.rollback()
        db_session.execute(delete(Distributor).where(Distributor.id == theirs.id))
        db_session.commit()


# --- Fees and charges ----------------------------------------------------------------


@pytest.mark.parametrize(
    "description, pack, charge",
    [
        ("FUEL SURCHARGE", None, True),
        ("DELIVERY FEE", "", True),
        ("VOLUME DISCOUNT 2%", None, True),
        ("BOTTLE DEPOSIT", None, True),
        ("CHEESE MOZZ SHRD", None, False),
        # A product line has a pack; its name can't make it a charge.
        ("DELIVERY BAGS PAPER", "500 CT", False),
        ("BAG DELIVERY PIZZA INSUL", None, False),
        ("DISC SANDING 5IN", None, False),
        ("DELIVERY CHARGE", None, True),
    ],
)
def test_charges_are_recognized_by_wording_on_lines_with_no_pack(description, pack, charge):
    assert is_charge(description, pack) is charge


def test_a_fee_line_isnt_left_on_match_items(db_session, tenant, monkeypatch):
    with_fee = FAKE_PAYLOAD.model_copy(
        update={
            "invoice_number": "FEE-1",
            "subtotal": "160.50",
            "total": "160.50",
            "line_items": [
                *FAKE_PAYLOAD.line_items,
                ExtractedLineItem(
                    line_number=3, raw_description="FUEL SURCHARGE", quantity="1", uom="EA",
                    unit_price="18.00", extended_price="18.00", confidence=0.99,
                ),  # fmt: skip
            ],
        }
    )
    invoice_id = _read(monkeypatch, tenant, _pdf("fee"), with_fee)
    fee = db_session.scalar(
        select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id, InvoiceLineItem.line_number == 3)
    )
    assert fee.review_status == ReviewStatus.not_product
    assert str(fee.id) not in _queue(tenant)


def test_a_person_can_mark_a_line_as_not_a_product_and_take_it_back(db_session, tenant, distributor, canonical_sku):
    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.review_status = ReviewStatus.pending
    db_session.commit()
    client = TestClient(app)

    marked = client.post(f"/review/{line.id}/not-product", params={"tenant_id": str(tenant.id)})
    assert marked.status_code == 200 and marked.json()["review_status"] == "not_product"
    assert str(line.id) not in _queue(tenant)

    back = client.post(f"/review/{line.id}/reopen", params={"tenant_id": str(tenant.id)})
    assert back.status_code == 200 and back.json()["review_status"] == "pending"


# --- Prices that aren't prices ---------------------------------------------------------


@pytest.mark.parametrize(
    "quantity, unit_price",
    [("1", "0.00"), ("-2", "69.02"), ("0", "14.00")],
    ids=["free promo case", "return", "out of stock"],
)
def test_free_returned_and_out_of_stock_lines_add_no_price(db_session, tenant, distributor, canonical_sku, quantity, unit_price):
    from app.models import Tenant, build_price_observation

    line = _auto_matched_line(db_session, tenant, distributor, canonical_sku)
    line.quantity, line.unit_price, line.normalized_unit_price = Decimal(quantity), Decimal(unit_price), Decimal(unit_price)
    invoice = db_session.get(Invoice, line.invoice_id)
    assert build_price_observation(line, invoice, db_session.get(Tenant, tenant.id)) is None


# --- Found reviewing the above ----------------------------------------------------------


def test_a_file_that_couldnt_be_read_can_be_added_again(db_session, tenant):
    data = _pdf("unreadable once")
    first = _post(tenant, data)
    assert _post(tenant, data).json()["detail"] == "You've already added this file. It's being read now."
    bind_tenant(db_session, tenant.id)
    db_session.get(Invoice, uuid.UUID(first.json()["id"])).status = InvoiceStatus.failed
    db_session.commit()
    assert _post(tenant, data).status_code == 201


def test_an_unknown_document_label_is_read_as_an_invoice(db_session, tenant, monkeypatch):
    # Structured output now allows only the five; a stored reading may not.
    odd = FAKE_PAYLOAD.model_copy(update={"document_type": "receipt", "invoice_number": "R-1"})
    invoice_id = _read(monkeypatch, tenant, _pdf("receipt"), odd)
    assert _detail(tenant, invoice_id)["document_type"] is None


def test_a_location_keeps_its_distributors_when_it_joins_an_account(db_session, tenant, own_distributors):
    from app.models import Account, Tenant

    client = TestClient(app)
    added = client.post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "Harbor Prime Meats"}).json()
    account = Account(id=uuid.uuid4(), name=f"Owner {uuid.uuid4().hex[:6]}")
    db_session.add(account)
    db_session.flush()
    db_session.get(Tenant, tenant.id).account_id = account.id
    db_session.commit()
    try:
        names = {d["id"] for d in client.get("/distributors", params={"tenant_id": str(tenant.id)}).json()}
        assert added["id"] in names
        again = client.post("/distributors", params={"tenant_id": str(tenant.id)}, json={"name": "HARBOR PRIME MEATS"})
        assert again.json()["id"] == added["id"]
    finally:
        db_session.get(Tenant, tenant.id).account_id = None
        db_session.commit()
        db_session.execute(delete(Account).where(Account.id == account.id))
        db_session.commit()


def test_deleting_the_original_lets_its_copy_be_matched_or_held_against_the_next(db_session, tenant, monkeypatch):
    original = _read(monkeypatch, tenant, _pdf("original"), FAKE_PAYLOAD)
    first_copy = _read(monkeypatch, tenant, _pdf("copy one"), FAKE_PAYLOAD)
    second_copy = _read(monkeypatch, tenant, _pdf("copy two"), FAKE_PAYLOAD)

    assert TestClient(app).delete(f"/invoices/{original}", params={"tenant_id": str(tenant.id)}).status_code == 204

    db_session.expire_all()
    bind_tenant(db_session, tenant.id)
    assert db_session.get(Invoice, first_copy).duplicate_of_id is None
    assert db_session.get(Invoice, second_copy).duplicate_of_id == first_copy
    # The one that's no longer held has been matched: nothing left unmatched
    # for want of trying.
    lines = db_session.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == first_copy)).all()
    assert all(li.canonical_sku_id is not None or li.match_confidence is not None for li in lines)


def test_adding_a_distributor_someone_just_added_returns_theirs(db_session, tenant, own_distributors, monkeypatch):
    from app import business_distributors

    first, created = business_distributors.add(db_session, tenant.id, "BulkMart Cash & Carry")
    db_session.commit()
    assert created
    # As if the lookup ran before the other person's row existed.
    real = business_distributors.find_by_name
    calls = []
    monkeypatch.setattr(
        business_distributors,
        "find_by_name",
        lambda db, t, n: None if not calls and not calls.append(1) else real(db, t, n),
    )
    again, created_again = business_distributors.add(db_session, tenant.id, "Bulkmart Cash and Carry")
    assert (again.id, created_again) == (first.id, False)
