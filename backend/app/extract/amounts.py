"""Reading amounts and quantities as invoices print them.

The model is asked for plain decimals (app/extract/prompt.py) but copies the
printed form often enough that the app can't rely on it: on the first set of
realistic test invoices, "$3,557.97" came back for totals on 45 of 97, and the
worker, which only read plain decimals, failed every one of them.
"""
import re
from decimal import Decimal, InvalidOperation

_NEGATIVE_MARKS = re.compile(r"\s*(CR|CREDIT)\.?\s*$", re.IGNORECASE)


def parse_amount(text: str | None) -> Decimal | None:
    """The number, or None when there's none to read (blank, illegible).

    Accepts "$1,234.50", "1,234.50", "-12.50", "(12.50)", "12.50-",
    "12.50 CR", a Unicode minus sign, and spaces after the dollar sign."""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    negative = False
    if _NEGATIVE_MARKS.search(s):
        s, negative = _NEGATIVE_MARKS.sub("", s), True
    s = s.replace("−", "-").replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("(") and s.endswith(")"):
        s, negative = s[1:-1], True
    if s.endswith("-"):
        s, negative = s[:-1], not negative
    if s.startswith("-"):
        s, negative = s[1:], not negative
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return -value if negative else value
