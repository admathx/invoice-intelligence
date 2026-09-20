"""Phase 3a gate, per SPEC.md §10: exhaustive table of every format in the
synthetic corpus plus known-adversarial cases. Binary, not a threshold —
100% must pass. A pack-size error produces a confidently wrong benchmark,
the single worst failure the product can have.
"""
from decimal import Decimal

import pytest

from app.models.enums import BaseUom
from app.normalize.pack_size import PackSizeParseError, parse_pack_size

# --- Every distinct raw_pack_size format actually present in synthetic/out/ ---
# (verified via: every tenant_*/week_*.json's line_items[].raw_pack_size)
CORPUS_CASES = [
    ("10 LB", "lb", "10"),
    ("10/1 DZ", "dz", "10"),
    ("100/1 CT", "ea", "100"),
    ("12/1 CT", "ea", "12"),
    ("12/16 OZ", "oz", "192"),
    ("12/8 OZ", "oz", "96"),
    ("15/1 DZ", "dz", "15"),
    ("2/1 GAL", "gal", "2"),
    ("2/10 LB", "lb", "20"),
    ("24/12 OZ", "oz", "288"),
    ("25 LB", "lb", "25"),
    ("250/1 CT", "ea", "250"),
    ("4/1 GAL", "gal", "4"),
    ("4/5 LB", "lb", "20"),
    ("40 LB", "lb", "40"),
    ("50/1 CT", "ea", "50"),
    ("6/1 GAL", "gal", "6"),
    ("6/12 CT", "ea", "72"),
    ("6/32 OZ", "oz", "192"),
    ("6/5 LB", "lb", "30"),
]


@pytest.mark.parametrize("raw,expected_unit,expected_base_units", CORPUS_CASES)
def test_corpus_formats(raw, expected_unit, expected_base_units):
    result = parse_pack_size(raw)
    assert result.unit == expected_unit
    assert result.base_units_per_case == Decimal(expected_base_units)


# --- Adversarial / spec-example cases not present in the synthetic corpus ---


def test_spec_example_can_size():
    # SPEC.md §6's own worked example.
    result = parse_pack_size("6/#10 CAN")
    assert result.unit == "oz"
    assert result.base_units_per_case == Decimal("660")


def test_can_size_lowercase_and_spacing():
    result = parse_pack_size("6 / # 10 can")
    assert result.base_units_per_case == Decimal("660")


def test_unknown_can_size_raises():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("6/#99 CAN")


def test_fractional_per_unit_weight():
    # "handle ... fractional weights" — decimal notation, e.g. 4 units of 2.5 lb each.
    result = parse_pack_size("4/2.5 LB")
    assert result.unit == "lb"
    assert result.base_units_per_case == Decimal("10.0")


def test_single_fractional_weight_no_case_prefix():
    result = parse_pack_size("0.5 LB")
    assert result.base_units_per_case == Decimal("0.5")


def test_case_insensitive():
    assert parse_pack_size("4/5 lb").base_units_per_case == Decimal("20")
    assert parse_pack_size("4/5 Lb").base_units_per_case == Decimal("20")


def test_extra_whitespace():
    assert parse_pack_size("  4 / 5   LB  ").base_units_per_case == Decimal("20")


def test_lbs_and_doz_synonyms():
    assert parse_pack_size("10 LBS").unit == "lb"
    assert parse_pack_size("5/1 DOZ").unit == "dz"


def test_each_synonyms():
    assert parse_pack_size("12 EA").unit == "ea"
    assert parse_pack_size("12 EACH").unit == "ea"
    assert parse_pack_size("12 CT").unit == "ea"


def test_oz_is_ambiguous_between_weight_and_fluid():
    result = parse_pack_size("12/16 OZ")
    assert result.compatible_base_uoms == {BaseUom.oz, BaseUom.fl_oz}


def test_gal_is_unambiguous():
    result = parse_pack_size("2/1 GAL")
    assert result.compatible_base_uoms == {BaseUom.gal}


def test_unknown_unit_raises():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("4/5 XYZ")


def test_garbage_raises():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("not a pack size")


def test_empty_string_raises():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("")


def test_missing_unit_raises():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("4/5")


def test_never_returns_zero_for_valid_input():
    # A pack size that parses to 0 base units would divide-by-zero downstream
    # (price per base unit) — every valid parse must be strictly positive.
    for raw, _, _ in CORPUS_CASES:
        assert parse_pack_size(raw).base_units_per_case > 0


def test_multiple_decimal_points_raises_cleanly():
    # `[\d.]+` used to match these, then Decimal() raised
    # decimal.InvalidOperation — which is NOT a ValueError, so it escaped
    # every `except PackSizeParseError` and failed the whole invoice
    # instead of just this line.
    for raw in ("4/5.5.5 LB", "5.5.5 LB", "4/5..5 LB", "4/. LB"):
        with pytest.raises(PackSizeParseError):
            parse_pack_size(raw)


def test_leading_decimal_point_raises_cleanly():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("4/.5 LB")
