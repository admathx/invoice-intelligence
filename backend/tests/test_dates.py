"""Invoice dates in the formats invoices print them (app/extract/dates.py)."""
from datetime import date

import pytest

from app.extract.dates import parse_invoice_date


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("2026-04-06", date(2026, 4, 6)),
        ("04/06/2026", date(2026, 4, 6)),  # US: month first
        ("4/6/26", date(2026, 4, 6)),
        ("04-06-2026", date(2026, 4, 6)),
        ("Apr 6, 2026", date(2026, 4, 6)),
        ("April 6,2026", date(2026, 4, 6)),
        ("06-APR-2026", date(2026, 4, 6)),
        (" 07/07/2026 ", date(2026, 7, 7)),
    ],
)
def test_the_formats_invoices_print(printed, expected):
    assert parse_invoice_date(printed) == expected


@pytest.mark.parametrize("printed", [None, "", "   ", "TBD", "13/45/2026", "04/06/0026", "2206-04-06"])
def test_missing_unreadable_or_implausible_dates_are_none(printed):
    assert parse_invoice_date(printed) is None
