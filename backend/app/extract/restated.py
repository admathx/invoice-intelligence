"""Rows whose arithmetic is printed another way.

The check that guards every invoice is quantity x price = amount, row by
row. Some rows are right and don't multiply out as read, because the page
says it another way: the amount cell says N/C, the weight that was billed
sits in the pack column, the price is per hundred, a deposit column is
added into each amount, the amount is left for a note below. (And one kind
that multiplies out and still can't be priced: a jug sold out of a case.) Each was held
for a person, on invoices where nothing had been misread.

Such a row is restated here, before the check, into the three numbers that
do multiply out. Only on evidence the page itself gives, and only when the
restated row then adds up to the cent: a row that still doesn't is left as
it was read, and is held as before. Nothing is worked out from the numbers
alone. Quantity x price / 100 = amount would explain a price per hundred,
and would as readily explain a quantity of 3.00 read as 300.
"""
import re
from decimal import Decimal

from app.extract.amounts import parse_amount
from app.extract.confidence import ARITHMETIC_TOLERANCE, check_arithmetic
from app.extract.schema import ExtractedInvoice, ExtractedLineItem
from app.normalize.pack_size import SPLIT_CASE_UNIT, PackSizeParseError, parse_pack_size

# What a price or amount cell says when nothing is charged for the row.
_NO_CHARGE = re.compile(r"FREE|N/?C|NO\s*CHARGE|NO\s*CHG|N/CHG|GRATIS|COMP(?:LIMENTARY)?")
# A scale weight in the pack column: "47.30 LB". Never a whole number, which
# is a pack size ("25 LB"): ten cases of a 10 lb pack must not become 10 lb.
_SCALE_WEIGHT = re.compile(r"(\d+\.\d+)\s*(?:LBS?|#)")
_CENT = Decimal("0.01")
# What a price is quoted per, when the page says so beside it ("12.00 /C"),
# and how many of what the quantity counts that is. The quantity is restated
# in that unit, under a name no invoice uses for anything else: C alone is
# also how some print "case".
_PRICED_PER = {
    "C": (100, "HUNDRED"),
    "HUNDRED": (100, "HUNDRED"),
    "M": (1000, "THOUSAND"),
    "THOUSAND": (1000, "THOUSAND"),
    "DZ": (12, "DZ"),
    "DOZ": (12, "DZ"),
    "DOZEN": (12, "DZ"),
}
_QUANTITY_PLACES = Decimal("0.0001")
DEPOSITS = "DEPOSIT, FROM THE DEPOSIT COLUMN"
# The fee a distributor charges for breaking a case, as its row names it.
_SPLIT_CASE_FEE = re.compile(r"\b(?:SPLIT|BROKEN|BREAK|BROKE)\s*(?:CASE|CS|PACK)\b|\bCASE\s*(?:SPLIT|BREAK)\b")


def _says_no_charge(text: str | None) -> bool:
    return bool(_NO_CHARGE.fullmatch((text or "").strip().upper().rstrip(".")))


def _multiplies_out(quantity: Decimal | None, price: Decimal | None, amount: Decimal | None) -> bool:
    return None not in (quantity, price, amount) and abs(quantity * price - amount) <= ARITHMETIC_TOLERANCE


def _no_charge(line: ExtractedLineItem) -> ExtractedLineItem | None:
    """A row given away: FREE or N/C where the price or the amount would
    be. Nothing was charged, so it counts for nothing and its price is
    nothing, whatever list price is printed beside it."""
    if not (_says_no_charge(line.unit_price) or _says_no_charge(line.extended_price)):
        return None
    amount = parse_amount(line.extended_price)
    if amount not in (None, Decimal(0)):
        return None  # FREE beside an amount that was charged: left for a person
    return line.model_copy(update={"unit_price": "0.00", "extended_price": "0.00"})


def _catch_weight(line: ExtractedLineItem) -> ExtractedLineItem | None:
    """A catch weight with its weight in the pack column: 2 cases, pack
    "47.30 LB", 9.80 a pound, amount 463.54. The quantity is the weight, as
    for any catch weight (app/extract/prompt.py). The weight printed is the
    whole delivery's, or one case's."""
    weight = _SCALE_WEIGHT.fullmatch((line.raw_pack_size or "").strip().upper())
    quantity, price, amount = (parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price))
    if weight is None or None in (quantity, price, amount):
        return None
    for pounds in (Decimal(weight.group(1)), Decimal(weight.group(1)) * quantity):
        if _multiplies_out(pounds, price, amount):
            return line.model_copy(update={"quantity": str(pounds), "uom": "LB"})
    return None


def _priced_per(line: ExtractedLineItem) -> ExtractedLineItem | None:
    """A price per hundred, thousand or dozen of what the quantity counts:
    300 at "12.00 /C" is 36.00. The quantity is restated in what the price
    is per (3 hundred at 12.00), as a catch weight's is in pounds."""
    per = _PRICED_PER.get((line.price_per or "").strip().upper().lstrip("/").strip())
    quantity, price, amount = (parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price))
    if per is None or None in (quantity, price, amount):
        return None
    restated = (quantity / per[0]).quantize(_QUANTITY_PLACES)
    if not _multiplies_out(restated, price, amount):
        return None
    # Plainly: str() of a normalized 30 is "3E+1", which is not a quantity.
    return line.model_copy(update={"quantity": format(restated.normalize(), "f"), "uom": per[1]})


def _deposit(line: ExtractedLineItem) -> tuple[ExtractedLineItem, Decimal] | None:
    """A row whose amount has a deposit from its own column added in: 1 at
    46.23 with 5.00 deposit is 51.23. The row's amount without it, and the
    deposit, which is counted as a charge of its own (restate)."""
    deposit = parse_amount(line.deposit)
    quantity, price, amount = (parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price))
    if not deposit or None in (quantity, price, amount) or not _multiplies_out(quantity, price, amount - deposit):
        return None
    return line.model_copy(update={"extended_price": str(amount - deposit)}), deposit


def _restated_row(line: ExtractedLineItem) -> tuple[str, ExtractedLineItem, Decimal] | None:
    """What kind of row it is, the row restated, and the deposit taken out
    of its amount; None for a row that multiplies out as read, or that
    nothing printed on it explains."""
    free = _no_charge(line)
    if free is not None:
        return "no_charge", free, Decimal(0)
    if _multiplies_out(*(parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price))):
        return None
    for kind, restated in (("priced_per", _priced_per(line)), ("catch_weight_in_pack", _catch_weight(line))):
        if restated is not None:
            return kind, restated, Decimal(0)
    with_deposit = _deposit(line)
    return ("deposit_column", *with_deposit) if with_deposit is not None else None


def _split_cases(lines: list[ExtractedLineItem]) -> list[int]:
    """Which rows are one container out of a split case: those billed by
    the each against a pack of several ("2 EA" of "6/1 GAL"), on an invoice
    with a split-case fee for each of them. "EA" against such a pack says
    nothing by itself (one jug, or the case?), so those rows were never
    priced; the fee says a case was broken, and for which rows when there
    are as many fees as rows it could mean."""
    fees = Decimal(0)
    of_a_case: list[int] = []
    for index, line in enumerate(lines):
        if not (line.raw_pack_size or "").strip() and _SPLIT_CASE_FEE.search(line.raw_description.upper()):
            fees += max(parse_amount(line.quantity) or Decimal(1), Decimal(1))
            continue
        if (line.uom or "").strip().upper() not in ("EA", "EACH"):
            continue
        try:
            if not parse_pack_size(line.raw_pack_size).is_single_container:
                of_a_case.append(index)
        except PackSizeParseError:
            continue
    return of_a_case if of_a_case and len(of_a_case) <= fees else []


def restate(extracted: ExtractedInvoice) -> tuple[ExtractedInvoice, list[str]]:
    """The reading with such rows restated, and what was restated (for the
    record). The reading itself when there is nothing to restate."""
    kinds: set[str] = set()
    lines: list[ExtractedLineItem] = []
    without_amount: list[int] = []
    deposits = Decimal(0)
    for index, line in enumerate(extracted.line_items):
        restated = _restated_row(line)
        if restated is not None:
            kind, line, deposit = restated
            kinds.add(kind)
            deposits += deposit
        else:
            quantity, price, amount = (parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price))
            if amount is None and quantity is not None and price is not None:
                # No amount on the row ("SEE BELOW", or a smudge): quantity x
                # price, if the invoice then adds up (below).
                without_amount.append(index)
                line = line.model_copy(update={"extended_price": str((quantity * price).quantize(_CENT))})
        lines.append(line)

    for index in _split_cases(lines):
        kinds.add("split_case")
        lines[index] = lines[index].model_copy(update={"uom": SPLIT_CASE_UNIT})

    if deposits:
        # The deposits are still money on the invoice: one charge, so the
        # rows add up to the subtotal as printed and each product's amount
        # is what the product cost.
        lines.append(
            ExtractedLineItem(
                line_number=max(line.line_number for line in lines) + 1,
                raw_description=DEPOSITS,
                quantity="1",
                uom="EA",
                unit_price=str(deposits),
                extended_price=str(deposits),
                confidence=1.0,
            )
        )

    if without_amount:
        # Kept only when the totals bear it out: they are the one check a
        # worked-out amount has, and as strict a one as the row's own.
        check = check_arithmetic(
            [
                (line.line_number, *(parse_amount(v) for v in (line.quantity, line.unit_price, line.extended_price)))
                for line in lines
            ],
            parse_amount(extracted.subtotal),
            parse_amount(extracted.tax),
            parse_amount(extracted.total),
        )
        if check.lines_sum_to_subtotal and check.totals_reconcile:
            kinds.add("amount_worked_out")
        else:
            for index in without_amount:
                lines[index] = extracted.line_items[index]

    if not kinds:
        return extracted, []
    return extracted.model_copy(update={"line_items": lines}), sorted(kinds)
