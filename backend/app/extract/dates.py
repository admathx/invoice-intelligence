"""Reading the dates an invoice prints.

The model is asked for YYYY-MM-DD (app/extract/prompt.py) but copies what's
printed often enough that the worker can't rely on it: on the first set of
realistic test invoices it returned "07/07/2026" for 95 of 97, and the
worker, which only accepted YYYY-MM-DD, would have marked every one of them
"Couldn't read". US distributors print month first, so "04/06/2026" is
April 6.
"""
from datetime import date, datetime

_FORMATS = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%m-%d-%y",
    "%m.%d.%Y",
    "%Y/%m/%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%b %d %Y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d %b %Y",
)


def parse_invoice_date(text: str | None) -> date | None:
    """The date, or None if there isn't one or it can't be read as one (a
    person then fills it in; the invoice is held until they do)."""
    if not text or not text.strip():
        return None
    cleaned = " ".join(text.strip().replace(",", ", ").split()).replace(" ,", ",")
    for fmt in _FORMATS:
        try:
            parsed = datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
        # A misread year (0026, 2206) is worse than none: it would put the
        # invoice's prices in the wrong place on every chart.
        return parsed if 2000 <= parsed.year <= date.today().year + 1 else None
    return None
