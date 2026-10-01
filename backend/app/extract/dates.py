"""Reading the dates an invoice prints.

The model is asked for YYYY-MM-DD (app/extract/prompt.py) but copies what's
printed often enough that the worker can't rely on it: on the first set of
realistic test invoices it returned "07/07/2026" for 95 of 97, and the
worker, which only accepted YYYY-MM-DD, would have marked every one of them
"Couldn't read". US distributors print month first, so "04/06/2026" is
April 6.
"""
from datetime import date, datetime, timedelta

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


# A date this far past the day the invoice arrived isn't one: a mistyped
# year ("2027" for "2026"), or a due date read as the invoice date. A few
# days' grace, since some distributors date an invoice for its delivery.
MAX_DAYS_AHEAD = 7


def dated_ahead(invoice_date: date | None, received: date) -> bool:
    """Whether the invoice is dated after it arrived. Left in, its prices
    would sit at the end of every price history until that day came."""
    return invoice_date is not None and invoice_date > received + timedelta(days=MAX_DAYS_AHEAD)
