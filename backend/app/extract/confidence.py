"""SPEC.md §5: arithmetic validation is the confidence signal that actually works.

For each line: quantity * unit_price ~= extended_price, within a cent. Then line
extendeds must sum to subtotal, and subtotal + tax ~= total. Any failure routes
the invoice to needs_review rather than extracted — this catches OCR digit
errors far more reliably than asking the model how sure it is.
"""
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

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
    status: InvoiceStatus = InvoiceStatus.extracted

    @property
    def reasons(self) -> list[str]:
        out = []
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
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def assess_extraction(extracted: ExtractedInvoice) -> ExtractionAssessment:
    failed_line_numbers: list[int] = []
    low_confidence_line_numbers: list[int] = []

    for line in extracted.line_items:
        qty = _to_decimal_or_none(line.quantity)
        unit_price = _to_decimal_or_none(line.unit_price)
        extended = _to_decimal_or_none(line.extended_price)
        if qty is None or unit_price is None or extended is None:
            failed_line_numbers.append(line.line_number)
        elif abs(qty * unit_price - extended) > ARITHMETIC_TOLERANCE:
            failed_line_numbers.append(line.line_number)

        if line.confidence < LOW_CONFIDENCE_THRESHOLD:
            low_confidence_line_numbers.append(line.line_number)

    line_extended_values = [_to_decimal_or_none(li.extended_price) for li in extracted.line_items]
    subtotal = _to_decimal_or_none(extracted.subtotal)
    if subtotal is not None and all(v is not None for v in line_extended_values):
        line_sum = sum(line_extended_values, Decimal(0))
        lines_sum_to_subtotal = abs(line_sum - subtotal) <= ARITHMETIC_TOLERANCE
    else:
        lines_sum_to_subtotal = False

    tax = _to_decimal_or_none(extracted.tax)
    total = _to_decimal_or_none(extracted.total)
    if lines_sum_to_subtotal and tax is not None and total is not None:
        totals_reconcile = abs(subtotal + tax - total) <= ARITHMETIC_TOLERANCE
    else:
        totals_reconcile = False

    distributor_is_other = extracted.distributor == "other"

    needs_review = (
        bool(failed_line_numbers)
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
        status=InvoiceStatus.needs_review if needs_review else InvoiceStatus.extracted,
    )
