"""A credit memo printed with positive amounts.

Most credit memos print what's being credited as negative quantities and
amounts, and are read that way. Some print everything positive and leave
"CREDIT MEMO" at the top to say which way the money goes; read as printed,
one of those was counted as $157.72 of spending rather than a credit of it.
"""
from app.extract.amounts import parse_amount
from app.extract.schema import ExtractedInvoice


def _as_credit(text: str) -> str:
    """An amount as a credit: negative if it was printed positive."""
    value = parse_amount(text)
    return text if value is None or value <= 0 else str(-value)


def as_credits(extracted: ExtractedInvoice) -> ExtractedInvoice | None:
    """The credit memo with its quantities and amounts negative, when its
    rows were printed with none negative; None when there's nothing to
    change (not a credit memo, rows already negative, or rows mixed, which
    are left as printed). The totals follow the rows: some print the rows
    positive and only the total as a credit ("157.72 CR")."""
    if extracted.document_type != "credit_memo" or not extracted.line_items:
        return None
    amounts = [parse_amount(line.extended_price) for line in extracted.line_items]
    quantities = [parse_amount(line.quantity) for line in extracted.line_items]
    if any(a is None or a < 0 for a in amounts) or any(q is not None and q < 0 for q in quantities):
        return None
    if not any(a > 0 for a in amounts):
        return None
    return extracted.model_copy(
        update={
            "subtotal": _as_credit(extracted.subtotal),
            "tax": _as_credit(extracted.tax),
            "total": _as_credit(extracted.total),
            "line_items": [
                line.model_copy(
                    update={"quantity": _as_credit(line.quantity), "extended_price": _as_credit(line.extended_price)}
                )
                for line in extracted.line_items
            ],
        }
    )
