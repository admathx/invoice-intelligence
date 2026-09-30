"""Amounts as invoices print them (app/extract/amounts.py)."""
from decimal import Decimal

import pytest

from app.extract.amounts import parse_amount


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("1234.50", "1234.50"),
        ("$1,234.50", "1234.50"),
        ("$ 5.00", "5.00"),
        ("7,318.67", "7318.67"),
        ("-12.50", "-12.50"),
        ("(12.50)", "-12.50"),
        ("$(12.50)", "-12.50"),
        ("12.50-", "-12.50"),
        ("12.50 CR", "-12.50"),
        ("−12.50", "-12.50"),
        ("38.46", "38.46"),
        ("0.5432", "0.5432"),
        (".75", "0.75"),
    ],
)
def test_the_ways_invoices_print_numbers(printed, expected):
    assert parse_amount(printed) == Decimal(expected)


@pytest.mark.parametrize("printed", [None, "", "  ", "OUT", "N/C", "1.2.3", "$", "12,34a"])
def test_blank_or_unreadable_is_none(printed):
    assert parse_amount(printed) is None
