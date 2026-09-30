"""The unit a line is billed in (app/extract/units.py)."""
from decimal import Decimal

import pytest

from app.extract.units import billing_unit


@pytest.mark.parametrize(
    "uom, pack, qty, expected",
    [
        ("CS", "4/10 LB", "2", "CS"),
        ("lb", None, "38.46", "LB"),
        ("EA", "5 LB", "1", "EA"),  # a real unit, kept as printed
        ("4/1 GAL", "4/1 GAL", "8", "CS"),  # the pack size copied in
        ("4/10 LB", "4/10 LB", "3", "CS"),
        ("", "25 LB", "2", "CS"),  # nothing printed
        (None, None, "64.27", "LB"),  # a catch weight with no unit
        ("2-5LB AVG", "2-5LB AVG", "12.40", "LB"),
    ],
)
def test_billing_unit(uom, pack, qty, expected):
    assert billing_unit(uom, pack, Decimal(qty)) == expected
