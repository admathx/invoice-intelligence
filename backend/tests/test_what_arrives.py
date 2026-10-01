"""What restaurants send that the app took as one ordinary invoice: several
invoices in one file, a long till receipt, an invoice made out to another
restaurant, one dated in the future.
"""
import io
import uuid
from datetime import date, timedelta

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app import splitting
from app.billed_to import MIN_HISTORY, names_sibling, same_name
from app.extract.client import FAKE_PAYLOAD
from app.extract.dates import MAX_DAYS_AHEAD, dated_ahead
from app.ingest.photos import photos_to_pdf
from app.ingest.render import (
    TILE_HEIGHT,
    TILE_OVERLAP,
    TILE_WIDTH,
    pdf_of_pages,
    render_pages,
    tile_tops,
)
from app.main import app
from app.models import Account, Invoice, InvoiceLineItem, Tenant
from app.models.enums import InvoiceStatus, ReviewStatus, VolumeTier
from app.storage import page_names, read_uri
from app.workers.tasks import process_invoice

# The committing fixtures (the API and the worker open their own sessions).
from test_review_api import _FixedExtractor, _line_ids, db_session, tenant  # noqa: F401


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Invoices queued for reading, instead of a real queue."""
    ids: list[str] = []
    monkeypatch.setattr("app.queue.invoice_queue.enqueue", lambda _fn, invoice_id, **_: ids.append(invoice_id))
    return ids


def _pdf(pages: list[str], size: tuple[float, float] = (612, 792)) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=size, invariant=1)
    for text in pages:
        c.drawString(36, size[1] - 72, text)
        c.showPage()
    c.save()
    return buf.getvalue()


def _page_count(pdf: bytes) -> int:
    document = pdfium.PdfDocument(pdf)
    try:
        return len(document)
    finally:
        document.close()


def _read(monkeypatch, tenant, data: bytes, payload) -> uuid.UUID:
    resp = TestClient(app).post(f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", data, "application/pdf")})
    assert resp.status_code == 201, resp.text
    invoice_id = uuid.UUID(resp.json()["id"])
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(payload))
    process_invoice(str(invoice_id))
    return invoice_id


def _detail(tenant, invoice_id) -> dict:
    return TestClient(app).get(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)}).json()


def _numbered(n: int, **changes):
    """FAKE_PAYLOAD as a different invoice each time, so none is a copy."""
    return FAKE_PAYLOAD.model_copy(update={"invoice_number": f"WA-{n:04d}", **changes})


# --- A long receipt ------------------------------------------------------------


def test_an_ordinary_page_is_one_image(tmp_path):
    pages = render_pages(_pdf(["one", "two"]), tmp_path)
    assert [(p.path.name, p.pdf_page) for p in pages] == [("page_001.png", 0), ("page_002.png", 1)]


def test_a_tall_page_is_read_in_overlapping_sheet_sized_slices(tmp_path):
    """A 3 x 14 inch receipt rendered whole was 450 pixels wide."""
    from PIL import Image

    pages = render_pages(_pdf(["receipt"], size=(216, 1008)), tmp_path)
    assert len(pages) > 1 and {p.pdf_page for p in pages} == {0}
    assert [p.path.name for p in pages] == [f"page_{i:03d}.png" for i in range(1, len(pages) + 1)]
    sizes = [Image.open(p.path).size for p in pages]
    assert all(size == (TILE_WIDTH, TILE_HEIGHT) for size in sizes)


def test_slices_overlap_and_end_at_the_foot_of_the_page():
    tops = tile_tops(5950)
    assert tops[0] == 0 and tops[-1] + TILE_HEIGHT == 5950
    assert all(b - a <= TILE_HEIGHT - TILE_OVERLAP for a, b in zip(tops, tops[1:]))
    assert tile_tops(TILE_HEIGHT) == [0] and tile_tops(900) == [0]


def test_a_page_too_long_to_slice_finely_is_still_a_bounded_number_of_images(tmp_path):
    from app.ingest.render import MAX_TILES

    assert len(render_pages(_pdf(["scroll"], size=(100, 14000)), tmp_path)) <= MAX_TILES


def test_a_tall_photo_keeps_the_width_its_small_print_needs(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.effect_noise((1500, 6000), 40).convert("RGB").save(buf, "JPEG", quality=70)
    pdf = photos_to_pdf([buf.getvalue()])
    document = pdfium.PdfDocument(pdf)
    try:
        image = next(document[0].get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
        width, height = image.get_size()
    finally:
        document.close()
    assert (width, height) == (1500, 6000)  # not shrunk to 750 x 3000
    assert len(render_pages(pdf, tmp_path)) >= 3


# --- Several invoices in one file ----------------------------------------------


def test_a_plan_groups_pages_by_invoice():
    assert splitting.plan([1, 1, 2, 3], [0, 1, 2, 3]) == {1: [0, 1], 2: [2], 3: [3]}
    # Blank backs and repeats (0) belong to none.
    assert splitting.plan([1, 0, 2, 0], [0, 1, 2, 3]) == {1: [0], 2: [2]}
    # Slices of one tall page are that page.
    assert splitting.plan([1, 1, 2, 2], [0, 0, 1, 1]) == {1: [0], 2: [1]}


@pytest.mark.parametrize(
    "page_invoices, pdf_pages",
    [
        ([], [0, 1]),  # no answer
        ([1, 1], [0, 1]),  # one invoice
        ([1, 0], [0, 1]),  # one invoice and a blank back
        ([1, 2, 3], [0, 1]),  # not an answer about these pages
        ([1, 2], [0, 0]),  # two numbers for the slices of one tall page: it is one page, one invoice
        ([1, -1], [0, 1]),
        (list(range(1, 140)), list(range(139))),  # more invoices than a file is divided into
    ],
)
def test_a_plan_is_none_when_there_is_nothing_to_split_or_no_safe_way(page_invoices, pdf_pages):
    assert splitting.plan(page_invoices, pdf_pages) is None


def test_a_tall_pages_slices_are_one_page_whatever_numbers_they_were_given():
    """A long receipt in four slices at the end of a stack was numbered as
    four invoices, and the whole stack then went unseparated."""
    assert splitting.plan([1, 2, 3, 4, 5], [0, 1, 2, 2, 2]) == {1: [0], 2: [1], 3: [2]}
    assert splitting.plan([1, 0, 2, 3], [0, 1, 1, 1]) == {1: [0], 2: [1]}


def test_a_month_of_single_page_invoices_is_separated():
    pages = list(range(24))
    assert splitting.plan([n + 1 for n in pages], pages) == {n + 1: [n] for n in pages}


def test_what_cannot_be_separated():
    assert not splitting.cannot_be_separated([1, 1], [0, 1])  # one invoice
    assert not splitting.cannot_be_separated([1, 2, 2, 3], [0, 1, 2, 3])  # a stack
    assert splitting.cannot_be_separated([1, 2], [0, 1, 2])  # not about these pages
    assert splitting.cannot_be_separated([1, -1], [0, 1])
    assert splitting.cannot_be_separated(list(range(1, 140)), list(range(139)))


def test_several_invoices_that_cant_be_divided_are_not_read_as_the_first_alone(db_session, tenant, monkeypatch, queued):
    """More invoices than a file is divided into: kept for a person, with
    every page, not one invoice Ready and the rest gone."""
    monkeypatch.setattr("app.splitting.MAX_INVOICES_PER_FILE", 2)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(["A", "B", "C"]), "application/pdf")}
    )
    invoice_id = uuid.UUID(resp.json()["id"])
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(_numbered(71, page_invoices=[1, 2, 3])))
    with pytest.raises(Exception, match="several invoices that couldn't be told apart"):
        process_invoice(str(invoice_id))
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.failed
    assert len(page_names(invoice_id)) == 3
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == invoice_id)).all() == []


def test_two_numbers_for_one_photo_of_two_pages_side_by_side_is_still_one_invoice(db_session, tenant, monkeypatch):
    """Both pages of an invoice in a single photo: the reader answered [1, 1]
    for the one image and read every row. It was refused as "several
    invoices that couldn't be told apart"."""
    invoice_id = _read(monkeypatch, tenant, _pdf(["both pages, side by side"]), _numbered(72, page_invoices=[1, 1]))
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.extracted


def test_pages_can_be_taken_out_of_a_pdf():
    assert _page_count(pdf_of_pages(_pdf(["a", "b", "c"]), [1, 2])) == 2


def test_a_file_with_several_invoices_becomes_an_invoice_each(db_session, tenant, monkeypatch, queued):
    stack = _pdf(["invoice A", "invoice B page 1", "invoice B page 2", "invoice C"])
    first = _read(monkeypatch, tenant, stack, _numbered(1, page_invoices=[1, 2, 2, 3]))

    db_session.expire_all()
    parent = db_session.get(Invoice, first)
    assert parent.status == InvoiceStatus.extracted and parent.split_from_id is None
    # The review screen shows its own page, not the others'.
    assert page_names(first) == ["page_001.png"]

    others = db_session.scalars(
        select(Invoice).where(Invoice.split_from_id == first).order_by(Invoice.created_at, Invoice.id)
    ).all()
    assert len(others) == 2
    assert {o.status for o in others} == {InvoiceStatus.received}
    assert sorted(_page_count(read_uri(o.original_file_uri)) for o in others) == [1, 2]
    assert all(o.source == parent.source and o.file_sha256 for o in others)
    # Each is queued to be read on its own.
    assert sorted(queued[-2:]) == sorted(str(o.id) for o in others)

    assert _detail(tenant, first)["split_note"] == (
        "The file held 3 invoices. This is the first; the others were added on their own."
    )
    assert "file with other invoices" in _detail(tenant, others[0].id)["split_note"]


def test_an_invoice_taken_out_of_a_file_is_read_like_any_other_and_not_split_again(db_session, tenant, monkeypatch, queued):
    first = _read(monkeypatch, tenant, _pdf(["A", "B", "B2"]), _numbered(2, page_invoices=[1, 2, 2]))
    db_session.expire_all()
    other = db_session.scalars(select(Invoice).where(Invoice.split_from_id == first)).one()

    # Read on its own; even if the reader again says "two invoices", it stays one.
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(_numbered(3, page_invoices=[1, 2])))
    process_invoice(str(other.id))
    db_session.expire_all()
    assert db_session.get(Invoice, other.id).status == InvoiceStatus.extracted
    assert page_names(other.id) == ["page_001.png", "page_002.png"]
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == other.id)).all() == []
    assert len(db_session.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id)).all()) == 2


def test_a_file_that_cannot_be_taken_apart_is_left_whole_not_failed(db_session, tenant, monkeypatch, queued):
    """It rendered and was read (and paid for); only cutting out the other
    invoices' pages failed."""

    def broken(*_a, **_k):
        raise RuntimeError("pdfium: failed to import pages")

    monkeypatch.setattr("app.workers.tasks.pdf_of_pages", broken)
    before = len(queued)
    first = _read(monkeypatch, tenant, _pdf(["A", "B"]), _numbered(7, page_invoices=[1, 2]))
    db_session.expire_all()
    assert db_session.get(Invoice, first).status == InvoiceStatus.extracted
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == first)).all() == []
    assert page_names(first) == ["page_001.png", "page_002.png"] and len(queued) == before + 1  # only its own upload


def test_one_invoice_with_a_blank_back_page_is_not_split(db_session, tenant, monkeypatch):
    first = _read(monkeypatch, tenant, _pdf(["front", ""]), _numbered(4, page_invoices=[1, 0]))
    db_session.expire_all()
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == first)).all() == []
    assert page_names(first) == ["page_001.png", "page_002.png"]
    assert _detail(tenant, first)["split_note"] is None


def test_a_failed_read_leaves_none_of_the_other_invoices_behind(db_session, tenant, monkeypatch, queued):
    forgotten: list[uuid.UUID] = []
    real_forget = __import__("app.storage", fromlist=["forget_original"]).forget_original
    monkeypatch.setattr("app.workers.tasks.forget_original", lambda i: (forgotten.append(i), real_forget(i)))

    def fail(*_a, **_k):
        raise ValueError("couldn't be saved")

    monkeypatch.setattr("app.workers.tasks.find_original", fail)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(["A", "B"]), "application/pdf")}
    )
    invoice_id = resp.json()["id"]
    before = len(queued)
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(_numbered(5, page_invoices=[1, 2])))
    with pytest.raises(ValueError):
        process_invoice(invoice_id)

    db_session.expire_all()
    assert db_session.get(Invoice, uuid.UUID(invoice_id)).status == InvoiceStatus.failed
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == uuid.UUID(invoice_id))).all() == []
    assert len(forgotten) == 1 and len(queued) == before


def test_deleting_the_first_invoice_leaves_the_others(db_session, tenant, monkeypatch):
    first = _read(monkeypatch, tenant, _pdf(["A", "B"]), _numbered(6, page_invoices=[1, 2]))
    resp = TestClient(app).delete(f"/invoices/{first}", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 204, resp.text
    db_session.expire_all()
    left = db_session.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id)).all()
    assert len(left) == 1 and left[0].split_from_id is None


# --- Made out to another restaurant --------------------------------------------


def test_names_that_are_one_restaurant():
    assert same_name("HARBOR & PINE KITCHEN", "Harbor and Pine Kitchen, LLC")
    assert same_name("HARBOR & PINE", "Harbor & Pine Kitchen")  # a shortening
    assert same_name("HARBOR & PINE KITCHN", "Harbor & Pine Kitchen")  # a misreading
    assert not same_name("BLUE OAK KITCHEN", "Harbor & Pine Kitchen")
    assert not same_name("", "Harbor & Pine Kitchen")


def test_a_sibling_is_told_from_this_restaurant_by_the_words_that_differ():
    assert names_sibling("BLUE OAK KITCHEN", "Harbor & Pine Kitchen", ["Blue Oak Kitchen"])
    assert names_sibling("BLUE OAK NORTH", "Blue Oak Downtown", ["Blue Oak North"])
    assert names_sibling("BLUE OAK NORTH", "Blue Oak", ["Blue Oak North"])
    # The shared name alone says nothing about which.
    assert not names_sibling("BLUE OAK", "Blue Oak Downtown", ["Blue Oak North"])
    assert not names_sibling("BLUE OAK DOWNTOWN", "Blue Oak Downtown", ["Blue Oak North"])
    # The owner's company name, printed on every location's invoices.
    assert not names_sibling("OAK HOSPITALITY GROUP", "Blue Oak Downtown", ["Blue Oak North"])


@pytest.fixture()
def sibling(db_session, tenant):
    """Another restaurant of the same owner."""
    group = Account(name=f"Group {uuid.uuid4().hex[:8]}")
    db_session.add(group)
    db_session.commit()
    other = Tenant(
        name=f"Blue Oak Kitchen {uuid.uuid4().hex[:6]}", metro=tenant.metro, volume_tier=VolumeTier.under_500k, account_id=group.id
    )
    db_session.add(other)
    db_session.get(Tenant, tenant.id).account_id = group.id
    db_session.commit()
    db_session.info["_created"]["tenants"].append(other.id)
    yield other
    db_session.rollback()
    for t in (tenant, other):
        db_session.get(Tenant, t.id).account_id = None
    db_session.commit()
    db_session.delete(db_session.get(Account, group.id))
    db_session.commit()


def test_an_invoice_made_out_to_the_owners_other_restaurant_is_held(db_session, tenant, sibling, monkeypatch):
    invoice_id = _read(monkeypatch, tenant, _pdf(["theirs"]), _numbered(10, customer_name=sibling.name.upper()))
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.billed_elsewhere and invoice.status == InvoiceStatus.needs_review
    detail = _detail(tenant, invoice_id)
    assert detail["check"]["passes"] is False
    assert any("made out to" in reason and sibling.name.upper() in reason for reason in detail["check"]["reasons"])
    # Nothing on it is matched or waiting to be.
    lines = db_session.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)).all()
    assert all(line.canonical_sku_id is None for line in lines)
    queue = {i["id"] for i in TestClient(app).get("/review/queue", params={"tenant_id": str(tenant.id)}).json()}
    assert not {str(i) for i in _line_ids(db_session, invoice_id)} & queue

    # "It's ours": no longer held, and a later invoice with that name isn't either.
    resp = TestClient(app).post(f"/invoices/{invoice_id}/keep", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 200, resp.text
    assert resp.json()["billed_elsewhere"] is False
    assert not any("made out to" in reason for reason in resp.json()["check"]["reasons"])

    # And it sticks: one distributor account covering both restaurants bills
    # everything under the other's name, every week.
    next_week = _read(monkeypatch, tenant, _pdf(["theirs again"]), _numbered(12, customer_name=sibling.name.upper()))
    db_session.expire_all()
    assert db_session.get(Invoice, next_week).billed_elsewhere is False
    # From a distributor that hasn't been told, it is still held.
    other = _read(
        monkeypatch, tenant, _pdf(["us foods"]), _numbered(13, distributor="us_foods", customer_name=sibling.name.upper())
    )
    db_session.expire_all()
    assert db_session.get(Invoice, other).billed_elsewhere is True


def test_an_invoice_in_the_owners_company_name_is_not_held(db_session, tenant, sibling, monkeypatch):
    invoice_id = _read(monkeypatch, tenant, _pdf(["ours"]), _numbered(11, customer_name="OAK HOSPITALITY GROUP LLC"))
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).billed_elsewhere is False


def test_an_unfamiliar_name_is_held_only_against_what_the_distributor_has_called_them(db_session, tenant, monkeypatch):
    # A distributor's first invoices are how the name it uses is learned: none is held.
    for n in range(MIN_HISTORY):
        early = _read(monkeypatch, tenant, _pdf([f"early {n}"]), _numbered(20 + n, customer_name="HP HOSPITALITY LLC"))
        db_session.expire_all()
        assert db_session.get(Invoice, early).billed_elsewhere is False
    # The usual name, written a little differently.
    usual = _read(monkeypatch, tenant, _pdf(["usual"]), _numbered(30, customer_name="H.P. Hospitality"))
    # The name over the door.
    own = _read(monkeypatch, tenant, _pdf(["own"]), _numbered(31, customer_name=tenant.name.upper()))
    # No name printed.
    blank = _read(monkeypatch, tenant, _pdf(["blank"]), _numbered(32, customer_name=None))
    # Another distributor, billing a name not seen before: nothing to hold it against.
    other_vendor = _read(
        monkeypatch, tenant, _pdf(["us foods"]), _numbered(35, distributor="us_foods", customer_name="H&P RESTAURANT GROUP")
    )
    # Someone else's, from the distributor that has always called them HP Hospitality.
    stranger = _read(monkeypatch, tenant, _pdf(["stranger"]), _numbered(33, customer_name="RIVERSIDE GRILL"))
    db_session.expire_all()
    held = {name: db_session.get(Invoice, i).billed_elsewhere for name, i in
            {"usual": usual, "own": own, "blank": blank, "other_vendor": other_vendor, "stranger": stranger}.items()}  # fmt: skip
    assert held == {"usual": False, "own": False, "blank": False, "other_vendor": False, "stranger": True}

    # Kept, it becomes one of the usual names.
    TestClient(app).post(f"/invoices/{stranger}/keep", params={"tenant_id": str(tenant.id)})
    again = _read(monkeypatch, tenant, _pdf(["again"]), _numbered(34, customer_name="Riverside Grill"))
    db_session.expire_all()
    assert db_session.get(Invoice, again).billed_elsewhere is False


# --- Dated in the future -------------------------------------------------------


def test_a_few_days_ahead_is_allowed_and_more_is_not():
    today = date(2026, 9, 30)
    assert not dated_ahead(today + timedelta(days=MAX_DAYS_AHEAD), today)
    assert dated_ahead(today + timedelta(days=MAX_DAYS_AHEAD + 1), today)
    assert not dated_ahead(None, today) and not dated_ahead(date(2025, 9, 30), today)


def test_an_invoice_dated_next_year_is_held_until_the_date_is_corrected(db_session, tenant, monkeypatch):
    """A mistyped year put the invoice's prices at the end of every history."""
    ahead = date.today() + timedelta(days=365)
    invoice_id = _read(monkeypatch, tenant, _pdf(["2027"]), _numbered(40, invoice_date=ahead.isoformat()))
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.invoice_date == ahead and invoice.status == InvoiceStatus.needs_review
    detail = _detail(tenant, invoice_id)
    assert any("hasn't come yet" in reason for reason in detail["check"]["reasons"])
    confirm = TestClient(app).post(f"/invoices/{invoice_id}/confirm", params={"tenant_id": str(tenant.id)})
    assert confirm.status_code == 422

    fixed = TestClient(app).patch(
        f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)}, json={"invoice_date": date.today().isoformat()}
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["check"]["passes"] is True
    confirm = TestClient(app).post(f"/invoices/{invoice_id}/confirm", params={"tenant_id": str(tenant.id)})
    assert confirm.status_code == 200, confirm.text


def test_an_invoice_with_no_subtotal_printed_is_ready(db_session, tenant, monkeypatch):
    invoice_id = _read(monkeypatch, tenant, _pdf(["receipt"]), _numbered(41, subtotal="", tax=""))
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.extracted and invoice.subtotal is None
    assert _detail(tenant, invoice_id)["check"]["passes"] is True


def test_a_page_wrongly_counted_as_another_invoice_can_be_put_right(db_session, tenant, monkeypatch):
    """The reader says "two invoices" of one two-page invoice. The first then
    doesn't add up (its second page's items are missing), so it keeps every
    page for whoever fixes it; the second, read alone, has the same invoice
    number and says what it is."""
    short = _numbered(50, page_invoices=[1, 2], total="999.00")  # the items don't reach the total
    first = _read(monkeypatch, tenant, _pdf(["page 1", "page 2"]), short)
    db_session.expire_all()
    assert db_session.get(Invoice, first).status == InvoiceStatus.needs_review
    assert page_names(first) == ["page_001.png", "page_002.png"]

    other = db_session.scalars(select(Invoice).where(Invoice.split_from_id == first)).one()
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(_numbered(50)))
    process_invoice(str(other.id))
    detail = _detail(tenant, other.id)
    assert detail["duplicate_of_id"] == str(first) and detail["duplicate_is_same_file"] is True
    assert any("more pages of invoice WA-0050" in reason for reason in detail["check"]["reasons"])


def test_a_copy_from_another_file_is_still_called_a_copy(db_session, tenant, monkeypatch):
    first = _read(monkeypatch, tenant, _pdf(["one"]), _numbered(51))
    again = _read(monkeypatch, tenant, _pdf(["one, rescanned"]), _numbered(51))
    detail = _detail(tenant, again)
    assert detail["duplicate_of_id"] == str(first) and detail["duplicate_is_same_file"] is False
    assert any("copy of invoice WA-0051" in reason for reason in detail["check"]["reasons"])


# --- Credits, reissues and damaged files (the fourth test set) ------------------

from decimal import Decimal  # noqa: E402

from app.duplicates import reissue_of  # noqa: E402
from app.extract.charges import is_charge  # noqa: E402
from app.extract.credit_memo import as_credits  # noqa: E402


def test_a_credit_memo_printed_with_positive_amounts_counts_as_a_credit(db_session, tenant, monkeypatch):
    """Read as printed, it was $142.50 of spending."""
    memo = _numbered(60, document_type="credit_memo")
    invoice_id = _read(monkeypatch, tenant, _pdf(["credit memo"]), memo)
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.extracted
    assert (invoice.subtotal, invoice.total) == (Decimal("-142.50"), Decimal("-142.50"))
    lines = db_session.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)).all()
    assert all(line.quantity < 0 and line.extended_price < 0 and line.unit_price > 0 for line in lines)


def test_a_credit_memo_already_negative_or_an_invoice_is_left_as_printed():
    assert as_credits(FAKE_PAYLOAD) is None  # an invoice
    rows = [li.model_copy(update={"quantity": f"-{li.quantity}", "extended_price": f"-{li.extended_price}"}) for li in FAKE_PAYLOAD.line_items]
    negative = FAKE_PAYLOAD.model_copy(
        update={"document_type": "credit_memo", "total": "-142.50", "subtotal": "-142.50", "line_items": rows}
    )
    assert as_credits(negative) is None
    mixed = FAKE_PAYLOAD.model_copy(
        update={
            "document_type": "credit_memo",
            "line_items": [FAKE_PAYLOAD.line_items[0], FAKE_PAYLOAD.line_items[1].model_copy(update={"extended_price": "-47.50"})],
        }
    )
    assert as_credits(mixed) is None


def test_positive_rows_under_a_total_printed_as_a_credit_follow_the_total(db_session, tenant, monkeypatch):
    """Rows 95.00 and 47.50, TOTAL 142.50 CR: read, the rows and the total
    disagreed in sign and the memo was held for not adding up."""
    memo = _numbered(65, document_type="credit_memo", subtotal="142.50", tax="0.00", total="142.50 CR")
    invoice_id = _read(monkeypatch, tenant, _pdf(["credit memo, CR total"]), memo)
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.extracted
    assert (invoice.subtotal, invoice.tax, invoice.total) == (Decimal("-142.50"), Decimal("0"), Decimal("-142.50"))


def test_a_credit_memo_with_its_invoices_number_is_not_a_copy_of_it(db_session, tenant, monkeypatch):
    invoice_id = _read(monkeypatch, tenant, _pdf(["invoice"]), _numbered(61))
    credit = _numbered(61, document_type="credit_memo", subtotal="-142.50", total="-142.50")
    credit = credit.model_copy(
        update={"line_items": [li.model_copy(update={"quantity": f"-{li.quantity}", "extended_price": f"-{li.extended_price}"}) for li in credit.line_items]}
    )
    credit_id = _read(monkeypatch, tenant, _pdf(["its credit memo"]), credit)
    db_session.expire_all()
    assert db_session.get(Invoice, credit_id).duplicate_of_id is None
    assert db_session.get(Invoice, credit_id).status == InvoiceStatus.extracted
    # A second copy of the credit memo is still a copy of the first.
    again = _read(monkeypatch, tenant, _pdf(["the credit memo, rescanned"]), credit)
    db_session.expire_all()
    assert db_session.get(Invoice, again).duplicate_of_id == credit_id
    assert db_session.get(Invoice, invoice_id).duplicate_of_id is None


@pytest.mark.parametrize(
    "number, base",
    [("904718271-R", "904718271"), ("904718271 REV", "904718271"), ("0088214/CORR", "88214"), ("904718271", None),
     ("INV-4410", None), ("904718271-A", None), ("R", None)],  # fmt: skip
)
def test_a_rebills_number_names_the_invoice_it_reissues(number, base):
    assert reissue_of(number) == base


def test_an_original_that_arrives_after_its_rebill_is_held_too(db_session, tenant, monkeypatch):
    rebill = _read(monkeypatch, tenant, _pdf(["rebill first"]), _numbered(63, invoice_number="WA-0063-R"))
    original = _read(monkeypatch, tenant, _pdf(["original, found later"]), _numbered(63))
    # A longer number that merely starts the same is another invoice.
    longer = _read(monkeypatch, tenant, _pdf(["another"]), _numbered(64, invoice_number="WA-00631"))
    db_session.expire_all()
    assert db_session.get(Invoice, rebill).duplicate_of_id is None
    assert db_session.get(Invoice, original).duplicate_of_id == rebill
    assert db_session.get(Invoice, longer).duplicate_of_id is None
    detail = _detail(tenant, original)
    assert detail["duplicate_is_reissue"] is True
    assert any("an original and its reissue" in reason for reason in detail["check"]["reasons"])


def test_a_rebill_is_held_against_its_original_and_takes_its_place_when_that_is_deleted(db_session, tenant, monkeypatch):
    """Ready beside its original, the delivery was counted twice."""
    original = _read(monkeypatch, tenant, _pdf(["original"]), _numbered(62))
    rebill = _read(monkeypatch, tenant, _pdf(["rebill"]), _numbered(62, invoice_number="WA-0062-R"))
    db_session.expire_all()
    assert db_session.get(Invoice, rebill).duplicate_of_id == original
    detail = _detail(tenant, rebill)
    assert detail["duplicate_is_reissue"] is True
    assert any("invoice WA-0062" in reason and "an original and its reissue" in reason for reason in detail["check"]["reasons"])

    resp = TestClient(app).delete(f"/invoices/{original}", params={"tenant_id": str(tenant.id)})
    assert resp.status_code == 204, resp.text
    detail = _detail(tenant, rebill)
    assert detail["duplicate_of_id"] is None and not any("reissue" in r for r in detail["check"]["reasons"])


def test_a_pdf_cut_off_partway_is_refused_when_added(db_session, tenant):
    """Accepted, it failed a minute later as "Couldn't read", with nothing to say why."""
    whole = _pdf(["an invoice"] * 3)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", whole[: len(whole) // 2], "application/pdf")}
    )
    assert resp.status_code == 415
    assert "damaged" in resp.json()["detail"]
    assert db_session.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id)).all() == []


def test_empties_going_back_are_not_products():
    assert is_charge("EMPTY KEG RETURN", None) and is_charge("KEG RETURN", None) and is_charge("BOTTLE RETURN", "")
    assert not is_charge("PALE ALE KEG", None) and not is_charge("RETURN ENVELOPE #10", None)


# --- Too many pages -------------------------------------------------------------


def test_a_pdf_with_more_pages_than_an_invoice_ever_has_is_refused(db_session, tenant, monkeypatch):
    """A 500-page file was rendered and sent to the model whole."""
    from app.config import settings

    monkeypatch.setattr(settings, "max_invoice_pages", 3)
    client = TestClient(app)
    ok = client.post(f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(["1", "2", "3"]), "application/pdf")})
    assert ok.status_code == 201
    resp = client.post(f"/invoices?tenant_id={tenant.id}", files={"file": ("b.pdf", _pdf(["1", "2", "3", "4"]), "application/pdf")})
    assert resp.status_code == 413
    assert "4 pages; the limit is 3" in resp.json()["detail"]
    assert len(db_session.scalars(select(Invoice).where(Invoice.tenant_id == tenant.id)).all()) == 1


def test_more_page_images_than_can_be_read_at_once_is_not_sent_to_the_model(db_session, tenant, monkeypatch):
    """Long receipts cut into slices can pass the page limit; the read stops
    before it is paid for, and the pages are kept for a person."""

    class _Never:
        def extract(self, pages):
            raise AssertionError("the model was called")

    monkeypatch.setattr("app.workers.tasks.MAX_PAGE_IMAGES", 2)
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(["1", "2", "3"]), "application/pdf")}
    )
    invoice_id = uuid.UUID(resp.json()["id"])
    monkeypatch.setattr("app.workers.tasks.extractor", _Never())
    with pytest.raises(Exception, match="3 page images"):
        process_invoice(str(invoice_id))
    db_session.expire_all()
    invoice = db_session.get(Invoice, invoice_id)
    assert invoice.status == InvoiceStatus.failed and invoice.extraction_cost_usd == 0
    assert len(page_names(invoice_id)) == 3


def test_a_reading_that_doesnt_account_for_every_page_is_never_one_ready_invoice(db_session, tenant, monkeypatch, queued):
    """From any source of readings (the real reader refuses these itself):
    five pages, a page list for two."""
    resp = TestClient(app).post(
        f"/invoices?tenant_id={tenant.id}", files={"file": ("a.pdf", _pdf(["1", "2", "3", "4", "5"]), "application/pdf")}
    )
    invoice_id = uuid.UUID(resp.json()["id"])
    monkeypatch.setattr("app.workers.tasks.extractor", _FixedExtractor(_numbered(70, page_invoices=[1, 2])))
    with pytest.raises(Exception, match="accounts for 2 of 5 page images"):
        process_invoice(str(invoice_id))
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.failed
    assert len(page_names(invoice_id)) == 5  # every page kept for a person
    assert db_session.scalars(select(Invoice).where(Invoice.split_from_id == invoice_id)).all() == []
