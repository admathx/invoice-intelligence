"""Rows that are right on the page and don't multiply out as read. Three
layouts of the fifth test set were held for a person with nothing misread:
words where a number would be, prices by another measure, and a deposit
column added into each amount."""
from decimal import Decimal

import pytest

from app.extract.charges import is_charge
from app.extract.confidence import assess_extraction
from app.extract.restated import DEPOSITS, restate
from app.extract.schema import ExtractedInvoice, ExtractedLineItem
from app.extract.units import billing_unit
from app.models.enums import InvoiceStatus
from app.normalize.matcher import normalize_price


def _row(n: int, quantity: str, price: str, amount: str, pack: str | None = "4/5 LB", **more) -> ExtractedLineItem:
    return ExtractedLineItem(
        line_number=n, raw_description=f"ITEM {n}", raw_pack_size=pack, quantity=quantity, uom="CS",
        unit_price=price, extended_price=amount, confidence=1.0, **more,
    )  # fmt: skip


def _invoice(rows: list[ExtractedLineItem], subtotal: str) -> ExtractedInvoice:
    return ExtractedInvoice(
        distributor="sysco", invoice_number="1", invoice_date="2026-05-04", subtotal=subtotal, tax="0.00", total=subtotal,
        line_items=rows,
    )  # fmt: skip


def _status(invoice: ExtractedInvoice) -> InvoiceStatus:
    return assess_extraction(invoice, distributor_known=True).status


def _per_unit(row: ExtractedLineItem) -> Decimal | None:
    quantity = Decimal(row.quantity)
    uom = billing_unit(row.uom, row.raw_pack_size, quantity)
    return normalize_price(row.raw_pack_size, quantity, Decimal(row.unit_price), uom)[1]


def test_an_ordinary_invoice_is_left_exactly_as_read():
    invoice = _invoice([_row(1, "2", "74.50", "149.00"), _row(2, "38.46", "4.89", "188.07")], "337.07")
    restated, kinds = restate(invoice)
    assert restated is invoice and kinds == []


@pytest.mark.parametrize("word", ["FREE", "N/C", "NC", "No Charge", "NO CHG", "n/c."])
def test_a_row_given_away_counts_for_nothing(word):
    """FREE where the price would be, or N/C where the amount would be, beside a list price."""
    invoice = _invoice([_row(1, "2", "74.50", "149.00"), _row(2, "2", word, "0.00"), _row(3, "3", "6.25", word)], "149.00")
    assert _status(invoice) == InvoiceStatus.needs_review
    restated, kinds = restate(invoice)
    assert kinds == ["no_charge"] and _status(restated) == InvoiceStatus.extracted
    assert [(r.unit_price, r.extended_price) for r in restated.line_items[1:]] == [("0.00", "0.00")] * 2


def test_free_beside_an_amount_that_was_charged_is_left_for_a_person():
    invoice = _invoice([_row(1, "2", "FREE", "149.00")], "149.00")
    restated, kinds = restate(invoice)
    assert kinds == [] and _status(restated) == InvoiceStatus.needs_review


def test_a_price_or_quantity_that_isnt_printed_is_not_worked_out():
    """MKT for a price, TBD for a quantity: the amount divided by the other
    would fill the cell, and nothing would then check either number."""
    invoice = _invoice([_row(1, "3", "MKT", "25.20"), _row(2, "TBD", "23.63", "94.52")], "119.72")
    restated, kinds = restate(invoice)
    assert kinds == [] and _status(restated) == InvoiceStatus.needs_review


def test_an_amount_left_off_the_row_is_quantity_times_price_when_the_totals_bear_it_out():
    rows = [_row(1, "2", "74.50", "149.00"), _row(2, "2", "33.31", "SEE BELOW"), _row(3, "1", "10.00", "")]
    restated, kinds = restate(_invoice(rows, "225.62"))
    assert kinds == ["amount_worked_out"] and _status(restated) == InvoiceStatus.extracted
    assert [r.extended_price for r in restated.line_items] == ["149.00", "66.62", "10.00"]


def test_an_amount_is_not_worked_out_when_the_totals_dont_bear_it_out():
    """A quantity misread beside a smudged amount: 3 for 2. The row would
    multiply out; the invoice wouldn't add up."""
    rows = [_row(1, "2", "74.50", "149.00"), _row(2, "3", "33.31", "")]
    restated, kinds = restate(_invoice(rows, "215.62"))
    assert kinds == [] and restated.line_items[1].extended_price == ""
    assert _status(restated) == InvoiceStatus.needs_review


def test_a_catch_weight_printed_in_the_pack_column_is_billed_by_the_pound():
    """2 cases, pack "47.30 LB", 9.80 a pound, amount 463.54."""
    restated, kinds = restate(_invoice([_row(1, "2", "9.80", "463.54", pack="47.30 LB")], "463.54"))
    row = restated.line_items[0]
    assert kinds == ["catch_weight_in_pack"] and (row.quantity, row.uom) == ("47.30", "LB")
    assert _status(restated) == InvoiceStatus.extracted and _per_unit(row) == Decimal("9.800000")
    # The weight of one case, printed for each of two.
    restated, _ = restate(_invoice([_row(1, "2", "9.80", "463.54", pack="23.65 LB")], "463.54"))
    assert (restated.line_items[0].quantity, restated.line_items[0].uom) == ("47.30", "LB")


def test_a_whole_number_pack_is_never_taken_for_a_weight():
    """Ten cases of a 10 lb pack at 4.00, the quantity misread as 16: ten
    pounds at 4.00 would explain the amount, and would be wrong."""
    restated, kinds = restate(_invoice([_row(1, "16", "4.00", "40.00", pack="10 LB")], "40.00"))
    assert kinds == [] and _status(restated) == InvoiceStatus.needs_review


@pytest.mark.parametrize(
    "quantity, price, per, amount, pack, restated_quantity, unit, each",
    [
        ("300", "12.00", "C", "36.00", "3000 CT", "3", "HUNDRED", "0.120000"),
        ("300", "12.00", "/C", "36.00", "3000 CT", "3", "HUNDRED", "0.120000"),
        ("2500", "68.00", "M", "170.00", "1000 CT", "2.5", "THOUSAND", "0.068000"),
        ("3000", "12.00", "C", "360.00", "3000 CT", "30", "HUNDRED", "0.120000"),  # 30, not "3E+1"
        ("36", "6.00", "DZ", "18.00", "15 DZ", "3", "DZ", "6.000000"),
    ],
)
def test_a_price_per_hundred_thousand_or_dozen_is_read_as_that(quantity, price, per, amount, pack, restated_quantity, unit, each):
    """300 napkins at "12.00 /C" is 36.00."""
    invoice = _invoice([_row(1, quantity, price, amount, pack=pack, price_per=per)], amount)
    assert _status(invoice) == InvoiceStatus.needs_review
    restated, kinds = restate(invoice)
    row = restated.line_items[0]
    assert kinds == ["priced_per"] and (row.quantity, row.uom, row.unit_price) == (restated_quantity, unit, price)
    assert _status(restated) == InvoiceStatus.extracted and _per_unit(row) == Decimal(each)


def test_a_price_per_hundred_is_never_worked_out_from_the_numbers():
    """A quantity of 3.00 read as 300 at 12.00 with amount 36.00 fits "per
    hundred" exactly. Without the page saying so, it stays held."""
    restated, kinds = restate(_invoice([_row(1, "300", "12.00", "36.00", pack="3000 CT")], "36.00"))
    assert kinds == [] and _status(restated) == InvoiceStatus.needs_review
    # And with the page saying so, only when it then multiplies out.
    restated, kinds = restate(_invoice([_row(1, "300", "12.00", "38.00", pack="3000 CT", price_per="C")], "38.00"))
    assert kinds == [] and _status(restated) == InvoiceStatus.needs_review


def test_a_bare_c_in_the_unit_column_is_not_a_hundred():
    """Some invoices print C for case. Only the name a restated row is given counts pieces."""
    assert normalize_price("3000 CT", Decimal("3"), Decimal("12.00"), "C")[1] is None
    assert normalize_price("3000 CT", Decimal("3"), Decimal("12.00"), "HUNDRED")[1] == Decimal("0.120000")
    assert normalize_price("1000 CT", Decimal("2"), Decimal("68.00"), "THOUSAND")[1] == Decimal("0.068000")


def test_deposits_added_into_each_amount_are_taken_out_and_counted_once():
    """A DEPOSIT column: 1 at 46.23 with 5.00 is 51.23; 4 at 87.55 with 10.00 is 360.20."""
    rows = [
        _row(1, "1", "46.23", "51.23", deposit="5.00"),
        _row(2, "4", "87.55", "360.20", deposit="10.00"),
        _row(3, "2", "20.00", "40.00"),  # no deposit on this row
    ]
    invoice = _invoice(rows, "451.43")
    assert _status(invoice) == InvoiceStatus.needs_review
    restated, kinds = restate(invoice)
    assert kinds == ["deposit_column"] and _status(restated) == InvoiceStatus.extracted
    assert [r.extended_price for r in restated.line_items] == ["46.23", "350.20", "40.00", "15.00"]
    deposits = restated.line_items[-1]
    assert deposits.raw_description == DEPOSITS and deposits.line_number == 4
    # A charge, not a product: it stays off Match items.
    assert is_charge(deposits.raw_description, deposits.raw_pack_size)


def test_a_deposit_that_doesnt_explain_the_amount_changes_nothing():
    rows = [_row(1, "1", "46.23", "52.23", deposit="5.00"), _row(2, "4", "87.55", "360.20", deposit="10.00")]
    restated, kinds = restate(_invoice(rows, "412.43"))
    assert kinds == ["deposit_column"]
    assert restated.line_items[0].extended_price == "52.23"  # as read, and it fails the check
    assert _status(restated) == InvoiceStatus.needs_review
    # A row that multiplies out already is left alone, whatever its deposit cell says.
    restated, kinds = restate(_invoice([_row(1, "2", "20.00", "40.00", deposit="5.00")], "40.00"))
    assert kinds == []


# --- Through the app -------------------------------------------------------------

from sqlalchemy import select  # noqa: E402

from app.extract.client import FAKE_PAYLOAD  # noqa: E402
from app.models import Invoice, InvoiceLineItem  # noqa: E402
from app.models.enums import ReviewStatus  # noqa: E402

from test_review_api import db_session, tenant  # noqa: E402, F401
from test_what_arrives import _pdf, _read, queued  # noqa: E402, F401


def test_an_invoice_with_a_deposit_column_is_ready_with_its_deposits_as_one_charge(db_session, tenant, monkeypatch):
    first = FAKE_PAYLOAD.line_items[0]
    rows = [
        first.model_copy(update={"line_number": 1, "quantity": "1", "unit_price": "46.23", "extended_price": "51.23", "deposit": "5.00"}),
        first.model_copy(update={"line_number": 2, "quantity": "4", "unit_price": "87.55", "extended_price": "360.20", "deposit": "10.00"}),
    ]  # fmt: skip
    payload = FAKE_PAYLOAD.model_copy(
        update={"invoice_number": "RS-0001", "line_items": rows, "subtotal": "411.43", "tax": "0.00", "total": "411.43"}
    )
    invoice_id = _read(monkeypatch, tenant, _pdf(["deposits"]), payload)
    db_session.expire_all()
    assert db_session.get(Invoice, invoice_id).status == InvoiceStatus.extracted
    lines = db_session.scalars(
        select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id).order_by(InvoiceLineItem.line_number)
    ).all()
    assert [line.extended_price for line in lines] == [Decimal("46.23"), Decimal("350.20"), Decimal("15.00")]
    assert lines[-1].raw_description == DEPOSITS and lines[-1].review_status == ReviewStatus.not_product


# --- A split case ----------------------------------------------------------------


def _split_invoice(rows):
    total = str(sum(Decimal(r.extended_price) for r in rows))
    return _invoice(rows, total)


def test_a_jug_sold_out_of_a_case_is_priced_when_the_invoice_charges_for_splitting_it():
    """"2 EA" of a "6/1 GAL" case at 9.25, with a SPLIT CASE FEE row: two
    jugs, 9.25 a gallon. "EA" alone doesn't say jug or case, and the row
    was never priced."""
    from app.normalize.pack_size import SPLIT_CASE_UNIT

    oil = _row(1, "2", "9.25", "18.50", pack="6/1 GAL").model_copy(update={"uom": "EA"})
    fee = _row(2, "1", "2.50", "2.50", pack=None).model_copy(update={"raw_description": "SPLIT CASE FEE", "uom": "EA"})
    whole = _row(3, "3", "117.99", "353.97", pack="4/10 LB")
    restated, kinds = restate(_split_invoice([oil, fee, whole]))
    assert kinds == ["split_case"]
    assert [r.uom for r in restated.line_items] == [SPLIT_CASE_UNIT, "EA", "CS"]
    assert normalize_price("6/1 GAL", Decimal("2"), Decimal("9.25"), SPLIT_CASE_UNIT) == (Decimal("2"), Decimal("9.250000"))
    # One 5 lb bag out of a case of four, at 12.50: 2.50 a pound.
    assert normalize_price("4/5 LB", Decimal("1"), Decimal("12.50"), SPLIT_CASE_UNIT) == (Decimal("5"), Decimal("2.500000"))
    assert _status(restated) == InvoiceStatus.extracted


def test_without_a_split_case_fee_each_against_a_case_is_still_not_guessed():
    oil = _row(1, "2", "9.25", "18.50", pack="6/1 GAL").model_copy(update={"uom": "EA"})
    restated, kinds = restate(_split_invoice([oil, _row(2, "3", "117.99", "353.97", pack="4/10 LB")]))
    assert kinds == [] and restated.line_items[0].uom == "EA"
    assert normalize_price("6/1 GAL", Decimal("2"), Decimal("9.25"), "EA") == (None, None)


def test_one_fee_doesnt_explain_two_rows_billed_by_the_each():
    """One could be the case: nothing says which."""
    oil = _row(1, "2", "9.25", "18.50", pack="6/1 GAL").model_copy(update={"uom": "EA"})
    mayo = _row(2, "1", "13.40", "13.40", pack="4/1 GAL").model_copy(update={"uom": "EA"})
    fee = _row(3, "1", "2.50", "2.50", pack=None).model_copy(update={"raw_description": "BROKEN CASE CHARGE", "uom": "EA"})
    restated, kinds = restate(_split_invoice([oil, mayo, fee]))
    assert kinds == []
    # A fee for each, or one fee row for two, does.
    two = fee.model_copy(update={"quantity": "2", "extended_price": "5.00"})
    restated, kinds = restate(_split_invoice([oil, mayo, two]))
    assert kinds == ["split_case"]
    # A single container is one whatever it's called, and a count is a count.
    bag = _row(1, "1", "24.48", "24.48", pack="25 LB").model_copy(update={"uom": "EA"})
    cups = _row(2, "5", "0.07", "0.35", pack="1000 CT").model_copy(update={"uom": "EA"})
    restated, kinds = restate(_split_invoice([bag, cups, fee]))
    assert kinds == []
