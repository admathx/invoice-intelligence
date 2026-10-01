"""A credit memo printed with positive amounts.

Most credit memos print what's being credited as negative quantities and
amounts, and are read that way. Some print everything positive and leave
"CREDIT MEMO" at the top to say which way the money goes; read as printed,
one of those was counted as $157.72 of spending rather than a credit of it.
"""
from app.extract.amounts import parse_amount
from app.extract.schema import ExtractedInvoice


def _negated(text: str) -> str:
    value = parse_amount(text)
    return text if value is None else str(-value)


def as_credits(extracted: ExtractedInvoice) -> ExtractedInvoice | None:
    """The credit memo with its quantities and amounts negative, when it was
    printed with none negative; None when there's nothing to change (not a
    credit memo, already negative, or mixed, which is left as printed)."""
    if extracted.document_type != "credit_memo" or not extracted.line_items:
        return None
    total = parse_amount(extracted.total)
    amounts = [parse_amount(line.extended_price) for line in extracted.line_items]
    quantities = [parse_amount(line.quantity) for line in extracted.line_items]
    if total is None or total <= 0 or any(a is None or a < 0 for a in amounts) or any(q is not None and q < 0 for q in quantities):
        return None
    return extracted.model_copy(
        update={
            "subtotal": _negated(extracted.subtotal),
            "tax": _negated(extracted.tax),
            "total": _negated(extracted.total),
            "line_items": [
                line.model_copy(update={"quantity": _negated(line.quantity), "extended_price": _negated(line.extended_price)})
                for line in extracted.line_items
            ],
        }
    )
