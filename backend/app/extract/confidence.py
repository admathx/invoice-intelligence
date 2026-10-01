"""SPEC.md §5: arithmetic validation is the confidence signal that actually works.

For each line: quantity * unit_price ~= extended_price, within a cent. Then line
extendeds must sum to subtotal, and subtotal + tax ~= total (with no subtotal
printed: the lines + tax ~= total). Any failure routes
the invoice to needs_review rather than extracted — this catches OCR digit
errors far more reliably than asking the model how sure it is.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from app.extract.amounts import parse_amount
from app.extract.schema import ExtractedInvoice
from app.models.enums import InvoiceStatus

ARITHMETIC_TOLERANCE = Decimal("0.01")
LOW_CONFIDENCE_THRESHOLD = 0.85


@dataclass
class ExtractionAssessment:
    failed_line_numbers: list[int] = field(default_factory=list)
    low_confidence_line_numbers: list[int] = field(default_factory=list)
    lines_sum_to_subtotal: bool = True
    totals_reconcile: bool = True
    distributor_is_other: bool = False
    no_line_items: bool = False
    status: InvoiceStatus = InvoiceStatus.extracted

    @property
    def reasons(self) -> list[str]:
        out = []
        if self.no_line_items:
            out.append("no line items extracted")
        if self.failed_line_numbers:
            out.append(f"line arithmetic failed: {self.failed_line_numbers}")
        if self.low_confidence_line_numbers:
            out.append(f"low-confidence lines: {self.low_confidence_line_numbers}")
        if not self.lines_sum_to_subtotal:
            out.append("line extendeds do not sum to subtotal")
        if not self.totals_reconcile:
            out.append("subtotal + tax does not reconcile to total")
        if self.distributor_is_other:
            out.append("distributor is 'other'")
        return out


def _to_decimal_or_none(value: str) -> Decimal | None:
    # As printed ("$1,234.50", "(12.50)"), or None when blank or illegible.
    return parse_amount(value)


@dataclass
class ArithmeticCheck:
    failed_line_numbers: list[int]
    lines_sum_to_subtotal: bool
    totals_reconcile: bool


# (line_number, quantity, unit_price, extended_price); None means unreadable.
ArithmeticLine = tuple[int, Decimal | None, Decimal | None, Decimal | None]


def check_arithmetic(
    lines: list[ArithmeticLine], subtotal: Decimal | None, tax: Decimal | None, total: Decimal | None
) -> ArithmeticCheck:
    """The arithmetic rules themselves, over numbers from any source.

    Split out of assess_extraction so the invoice review screen re-checks a
    human's corrections against exactly the rules that flagged the invoice in
    the first place (app/api/invoice_review.py). Two implementations of "does
    this invoice add up" would eventually disagree about some invoice, and
    then an invoice could be confirmed that the worker would have rejected.
    """
    failed_line_numbers = [
        line_number
        for line_number, qty, unit_price, extended in lines
        if qty is None or unit_price is None or extended is None
        or abs(qty * unit_price - extended) > ARITHMETIC_TOLERANCE
    ]

    extended_values = [extended for _, _, _, extended in lines]
    items_total = sum(extended_values, Decimal(0)) if all(v is not None for v in extended_values) else None
    if items_total is None:
        lines_sum_to_subtotal = False
    elif subtotal is None:
        # No subtotal to check against: a short invoice or a till receipt
        # prints only a total. The items are then checked against the total
        # below, which is as strict; requiring a subtotal held every such
        # invoice however well it was read.
        lines_sum_to_subtotal = True
    else:
        lines_sum_to_subtotal = abs(items_total - subtotal) <= ARITHMETIC_TOLERANCE

    if lines_sum_to_subtotal and total is not None:
        # No tax line is no tax. A tax that was printed but couldn't be read
        # still fails here, since the total includes it.
        before_tax = items_total if subtotal is None else subtotal
        totals_reconcile = abs(before_tax + (tax or Decimal(0)) - total) <= ARITHMETIC_TOLERANCE
    else:
        totals_reconcile = False

    return ArithmeticCheck(failed_line_numbers, lines_sum_to_subtotal, totals_reconcile)


def assess_extraction(extracted: ExtractedInvoice, distributor_known: bool | None = None) -> ExtractionAssessment:
    """`distributor_known` overrides what extraction said about the
    distributor: an invoice from a vendor the business added itself reads as
    'other' but has been recognized by name (app/business_distributors.py)."""
    arithmetic = check_arithmetic(
        [
            (
                line.line_number,
                _to_decimal_or_none(line.quantity),
                _to_decimal_or_none(line.unit_price),
                _to_decimal_or_none(line.extended_price),
            )
            for line in extracted.line_items
        ],
        _to_decimal_or_none(extracted.subtotal),
        _to_decimal_or_none(extracted.tax),
        _to_decimal_or_none(extracted.total),
    )
    failed_line_numbers = arithmetic.failed_line_numbers
    lines_sum_to_subtotal = arithmetic.lines_sum_to_subtotal
    totals_reconcile = arithmetic.totals_reconcile
    low_confidence_line_numbers = [
        line.line_number for line in extracted.line_items if line.confidence < LOW_CONFIDENCE_THRESHOLD
    ]

    distributor_is_other = (
        extracted.distributor == "other" if distributor_known is None else not distributor_known
    )

    # Checked explicitly, because every arithmetic check above passes
    # *vacuously* on an empty invoice: the per-line loop never runs,
    # `all(...)` over an empty list is True, and sum([]) == 0 reconciles
    # against zeroed totals. A blank or unreadable page that yields no line
    # items would otherwise ship as `extracted` — a worse outcome than the
    # wrong numbers this module exists to catch, since nothing downstream
    # would ever flag it.
    no_line_items = not extracted.line_items

    needs_review = (
        no_line_items
        or bool(failed_line_numbers)
        or bool(low_confidence_line_numbers)
        or not totals_reconcile
        or distributor_is_other
    )

    return ExtractionAssessment(
        failed_line_numbers=failed_line_numbers,
        low_confidence_line_numbers=low_confidence_line_numbers,
        lines_sum_to_subtotal=lines_sum_to_subtotal,
        totals_reconcile=totals_reconcile,
        distributor_is_other=distributor_is_other,
        no_line_items=no_line_items,
        status=InvoiceStatus.needs_review if needs_review else InvoiceStatus.extracted,
    )
