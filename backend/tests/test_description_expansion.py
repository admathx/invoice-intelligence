import pytest

from app.normalize.description_expansion import normalize_for_embedding


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("SHRMP 16/20 P&D TAIL ON", "SHRIMP 16 20 PEELED DEVEINED TAIL ON"),
        ("CHS MOZZ SHRD WM", "CHEESE MOZZARELLA SHREDDED WHOLE MILK"),
        ("CUP HOT PPR 16Z WHT", "CUP HOT PAPER 16OZ WHITE"),
        ("PORK BUTT BNLS", "PORK SHOULDER BONELESS"),
        ("ONION YLLW JBO 50#", "ONION YELLOW JUMBO 50"),
    ],
)
def test_distributor_shorthand_is_spelled_out(printed, expected):
    assert normalize_for_embedding(printed) == expected


def test_grn_is_grain_on_long_grain_rice_and_green_elsewhere():
    assert normalize_for_embedding("RICE LNG GRN") == "RICE LONG GRAIN"
    assert normalize_for_embedding("PEPPER BELL GRN") == "PEPPER BELL GREEN"


def test_solid_cheese_is_block_cheese():
    assert normalize_for_embedding("CHEESE CHEDDAR SOLID") == "CHEESE CHEDDAR BLOCK"
