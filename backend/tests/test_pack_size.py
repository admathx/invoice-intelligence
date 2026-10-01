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


# --- billed unit vs pack unit (matcher._apply_pack_size) -------------------

from app.normalize.matcher import _apply_pack_size  # noqa: E402
from app.normalize.pack_size import BilledUnitMismatchError  # noqa: E402


def test_a_line_billed_in_the_packs_own_unit_passes_through():
    qty, price = _apply_pack_size(parse_pack_size("4/5 LB"), Decimal("12"), Decimal("2.50"), "LB")
    assert (qty, price) == (Decimal("12"), Decimal("2.5000"))


def test_a_broken_case_billed_per_each_is_not_recorded_as_a_per_pound_price():
    """One 5 lb bag billed "EA" at $12.50 used to be recorded as $12.50/lb —
    five times the real $2.50/lb. Nothing on the line says how many pounds one
    EA holds, so it must refuse rather than guess a divisor.
    """
    with pytest.raises(BilledUnitMismatchError):
        _apply_pack_size(parse_pack_size("4/5 LB"), Decimal("1"), Decimal("12.50"), "EA")


def test_a_per_pound_line_against_a_can_pack_is_refused():
    """A per-pound price on an ounce-denominated SKU would be off by 16x."""
    with pytest.raises(BilledUnitMismatchError):
        _apply_pack_size(parse_pack_size("6/#10 CAN"), Decimal("1"), Decimal("3.00"), "LB")


def test_a_mismatch_is_a_pack_size_error_so_every_caller_routes_it_to_review():
    assert issubclass(BilledUnitMismatchError, PackSizeParseError)


def test_case_billing_is_unchanged():
    qty, price = _apply_pack_size(parse_pack_size("4/5 LB"), Decimal("2"), Decimal("50.00"), "CS")
    assert (qty, price) == (Decimal("40"), Decimal("2.5000"))


# --- pricing in the matched product's unit ----------------------------------

from types import SimpleNamespace  # noqa: E402

from app.normalize.pack_size import ProductUnitMismatchError  # noqa: E402


def _product(uom, lb_per_gal=None):
    """What _apply_pack_size reads from a CanonicalSku."""
    return SimpleNamespace(base_uom=uom, lb_per_gal=lb_per_gal)


def test_a_pack_in_dozens_is_priced_per_item_for_a_product_sold_each():
    """Bar towels come "12 DZ"; the catalog prices Bar Mop Towel each."""
    qty, price = _apply_pack_size(parse_pack_size("12 DZ"), Decimal("1"), Decimal("28.80"), "CS", _product(BaseUom.each))
    assert (qty, price) == (Decimal("144"), Decimal("0.2000"))


def test_fluid_ounces_are_priced_per_gallon_for_a_product_sold_by_the_gallon():
    qty, price = _apply_pack_size(parse_pack_size("6/128 OZ"), Decimal("1"), Decimal("30.00"), "CS", _product(BaseUom.gal))
    assert (qty, price) == (Decimal("6"), Decimal("5.0000"))


def test_ounces_are_priced_per_pound_for_a_product_sold_by_weight():
    _, price = _apply_pack_size(parse_pack_size("10/16 OZ"), Decimal("1"), Decimal("40.00"), "CS", _product(BaseUom.lb))
    assert price == Decimal("4.0000")


def test_weight_never_converts_to_volume():
    """A 35 lb jug of oil against oil priced per gallon needs a density."""
    with pytest.raises(ProductUnitMismatchError):
        _apply_pack_size(parse_pack_size("35 LB"), Decimal("1"), Decimal("40.67"), "CS", _product(BaseUom.gal))


def test_a_cans_ounces_are_net_weight_never_fluid():
    """#10 is 110 oz of product; read as fluid it's about 10% off a jug."""
    pack = parse_pack_size("6/#10 CAN")
    assert pack.compatible_base_uoms == {BaseUom.oz}
    assert BaseUom.gal not in pack.convertible_base_uoms and BaseUom.fl_oz not in pack.convertible_base_uoms
    with pytest.raises(ProductUnitMismatchError):
        _apply_pack_size(pack, Decimal("1"), Decimal("41.32"), "CS", _product(BaseUom.gal))


def test_each_against_a_pack_of_many_is_refused():
    """A case of 1000 cups billed "EA" at $65.93 was recorded as $65.93 a
    cup. EA doesn't say whether the price is for one piece or the case."""
    with pytest.raises(BilledUnitMismatchError):
        _apply_pack_size(parse_pack_size("1000 CT"), Decimal("3"), Decimal("65.93"), "EA", _product(BaseUom.each))


def test_each_against_a_single_item_pack_is_its_price():
    qty, price = _apply_pack_size(parse_pack_size("1 EA"), Decimal("4"), Decimal("12.50"), "EA", _product(BaseUom.each))
    assert (qty, price) == (Decimal("4"), Decimal("12.5000"))


# --- a line with no pack printed ---------------------------------------------

from app.normalize.pack_size import pack_for_line  # noqa: E402


def test_no_pack_billed_by_the_pound_is_priced_per_pound():
    """Salmon at $9.70 billed LB with no pack printed is $9.70 a pound."""
    qty, price = _apply_pack_size(pack_for_line(None, "LB"), Decimal("12.4"), Decimal("9.70"), "LB", _product(BaseUom.lb))
    assert (qty, price) == (Decimal("12.4"), Decimal("9.7000"))


@pytest.mark.parametrize("uom", ["CS", "EA", "BG"])
def test_no_pack_billed_by_the_case_or_count_is_still_unreadable(uom):
    with pytest.raises(PackSizeParseError):
        pack_for_line("", uom)


def test_a_printed_pack_wins_over_the_billing_unit():
    assert pack_for_line("4/5 LB", "LB").base_units_per_case == Decimal("20")


# --- weight against volume, through a product's weight per gallon ------------


def test_a_35_lb_jug_of_oil_is_priced_per_gallon_through_its_weight_per_gallon():
    """35 lb of canola at 7.7 lb/gal is 4.545 gal; $40.67 is $8.95 a gallon."""
    qty, price = _apply_pack_size(parse_pack_size("35 LB"), Decimal("1"), Decimal("40.67"), "CS", _product(BaseUom.gal, Decimal("7.7")))
    assert price == Decimal("8.9474")
    assert qty.quantize(Decimal("0.001")) == Decimal("4.545")


def test_a_can_of_ketchup_is_priced_per_gallon_through_its_weight():
    """6 x #10 (110 oz net) is 41.25 lb; at 9.5 lb/gal, 4.342 gal."""
    _, price = _apply_pack_size(parse_pack_size("6/#10 CAN"), Decimal("1"), Decimal("41.32"), "CS", _product(BaseUom.gal, Decimal("9.5")))
    assert price == Decimal("9.5161")


def test_gallons_are_priced_per_pound_for_a_weight_product_with_a_density():
    _, price = _apply_pack_size(parse_pack_size("6/1 GAL"), Decimal("1"), Decimal("46.20"), "CS", _product(BaseUom.lb, Decimal("7.7")))
    assert price == Decimal("1.0000")


def test_without_a_density_weight_and_volume_still_dont_compare():
    with pytest.raises(ProductUnitMismatchError):
        _apply_pack_size(parse_pack_size("35 LB"), Decimal("1"), Decimal("40.67"), "CS", _product(BaseUom.gal))


# --- rolls, packs of packs, pounds written "#", bags ------------------------

from app.normalize.pack_size import pack_from_description  # noqa: E402


@pytest.mark.parametrize(
    "raw, unit, amount",
    [
        ("1/500 FT", "ea", "1"),  # one roll of foil
        ("6/3 PK", "ea", "18"),  # six packs of three romaine hearts
        ("50#", "lb", "50"),
        ("4/5#", "lb", "20"),
    ],
)
def test_rolls_packs_and_pound_signs_parse(raw, unit, amount):
    parsed = parse_pack_size(raw)
    assert (parsed.unit, parsed.base_units_per_case) == (unit, Decimal(amount))


def test_six_ten_pound_sign_is_left_unreadable():
    """"6/10#" is as likely six #10 cans as six 10 lb bags."""
    with pytest.raises(PackSizeParseError):
        parse_pack_size("6/10#")


def test_a_bag_of_a_single_bag_pack_is_the_whole_pack():
    """Flour "50 LB" billed BG: every bag of flour and beans went unpriced."""
    _, price = _apply_pack_size(parse_pack_size("50 LB"), Decimal("1"), Decimal("24.50"), "BG", _product(BaseUom.lb))
    assert price == Decimal("0.4900")


def test_a_bag_of_a_pack_of_several_bags_is_refused():
    """"4/5 LB" billed BG: one 5 lb bag, or the whole case?"""
    with pytest.raises(BilledUnitMismatchError):
        _apply_pack_size(parse_pack_size("4/5 LB"), Decimal("1"), Decimal("12.50"), "BG", _product(BaseUom.lb))


@pytest.mark.parametrize(
    "description, uom, unit, amount",
    [
        ("BBQ SAUCE ORIGINAL 4/1 GAL", "CS", "gal", "4"),
        ("CHEESE AMERICAN SLICES 4/5 LB", "CS", "lb", "20"),
        ("BUTTER SOLID 36/1 LB", "CS", "lb", "36"),
        # A bare size is the thing billed when it's billed per bag or sack.
        ("FLOUR ALL PURPOSE 50 LB", "BG", "lb", "50"),
        ("POTATO RUSSET 50#", "SK", "lb", "50"),
    ],
)
def test_a_pack_written_in_the_description_is_read(description, uom, unit, amount):
    parsed = pack_from_description(description, uom)
    assert (parsed.unit, parsed.base_units_per_case, parsed.from_description) == (unit, Decimal(amount), True)


@pytest.mark.parametrize(
    "description, uom",
    [
        # Billed per case, a bare size is as often the unit inside the case.
        ("SOUR CREAM 5 LB", "CS"),
        ("MAYO HVY DTY 1 GAL", "CS"),
        ("EGG LARGE 15DZ", "CS"),
        # Four patties to the pound, not four 1 lb packs.
        ("PATTY BEEF 80/20 4/1 LB", "CS"),
        ("BURGER ANGUS 3/1 LB", "CS"),
    ],
)
def test_sizes_that_may_not_be_the_case_are_not_read_as_the_pack(description, uom):
    assert pack_from_description(description, uom) is None


def test_inches_are_a_size_not_a_roll():
    """"14 IN" on a pizza box put a case's price on one box."""
    with pytest.raises(PackSizeParseError):
        parse_pack_size("14 IN")


@pytest.mark.parametrize(
    "description",
    [
        "CUPS HOT PAPER 16 OZ",  # a cup size
        "CHICKEN BREAST BNLS 6OZ",  # a portion
        "CHEESE AMER SLCD 120CT",  # slices, not the case
        "KETCHUP FANCY 6/10#",  # cans or pounds
        "BURGER PATTY 1/3 LB",  # a third of a pound
        "BEEF GRND 80/20 4/10",  # no unit
        "SHRIMP 16/20",  # a count size
    ],
)
def test_sizes_that_arent_the_pack_are_not_read_from_the_description(description):
    assert pack_from_description(description, "BG") is None


def test_a_described_pack_only_prices_against_a_product_it_converts_to():
    pack = pack_for_line(None, "CS", "BAGS TRASH 2/33 GAL")
    with pytest.raises(ProductUnitMismatchError):
        _apply_pack_size(pack, Decimal("1"), Decimal("32.99"), "CS", _product(BaseUom.each))
    _, price = _apply_pack_size(pack_for_line(None, "CS", "BBQ SAUCE ORIGINAL 4/1 GAL"), Decimal("1"), Decimal("41.16"), "CS", _product(BaseUom.gal))
    assert price == Decimal("10.2900")


# --- quarts, pints, metric and kegs -------------------------------------------

from app.normalize.matcher import normalize_price  # noqa: E402
from app.normalize.pack_size import pack_from_description  # noqa: E402


@pytest.mark.parametrize(
    "raw, unit, amount",
    [
        ("12/1 QT", "gal", "3"),
        ("6/1 QT", "gal", "1.5"),
        ("12/1 PT", "gal", "1.5"),
        ("12/32 FL OZ", "gal", "3"),
        ("12/32 FL. OZ.", "gal", "3"),
        ("6/1 L", "gal", "1.585032"),
        ("12/750 ML", "gal", "2.377548"),
        ("1/5 KG", "lb", "11.0231"),
        ("12/500 G", "lb", "13.22772"),
        ("5 KG", "lb", "11.0231"),
    ],
)
def test_quarts_pints_and_metric_packs_are_read_as_gallons_and_pounds(raw, unit, amount):
    """Dairy comes in quarts, imported goods in metric; none could be priced."""
    parsed = parse_pack_size(raw)
    assert (parsed.unit, parsed.base_units_per_case) == (unit, Decimal(amount))


def test_a_case_of_quarts_is_priced_per_gallon():
    qty, price = _apply_pack_size(parse_pack_size("12/1 QT"), Decimal("2"), Decimal("48.00"), "CS", _product(BaseUom.gal))
    assert (qty, price) == (Decimal("6"), Decimal("16.0000"))


def test_explicit_fluid_ounces_are_never_weight():
    assert parse_pack_size("12/32 FL OZ").convertible_base_uoms == {BaseUom.gal, BaseUom.fl_oz}


def test_a_line_billed_per_quart_is_a_quarter_gallon_each():
    qty, price = _apply_pack_size(pack_for_line(None, "QT"), Decimal("8"), Decimal("4.00"), "QT", _product(BaseUom.gal))
    assert (qty, price) == (Decimal("2"), Decimal("16.0000"))
    # And against a printed pack of quarts.
    qty, price = _apply_pack_size(parse_pack_size("12/1 QT"), Decimal("8"), Decimal("4.00"), "QT", _product(BaseUom.gal))
    assert (qty, price) == (Decimal("2"), Decimal("16.0000"))


def test_a_line_billed_per_kilogram_is_priced_per_pound():
    qty, price = normalize_price(None, Decimal("10"), Decimal("22.0462"), "KG", _product(BaseUom.lb))
    assert qty == Decimal("22.0462") and price == Decimal("10.0000")


def test_a_keg_is_a_fraction_of_a_barrel_not_two_barrels():
    """"1/2 BBL" read as one container of two was 62 gallons."""
    assert parse_pack_size("1/2 BBL").base_units_per_case == Decimal("15.5")
    assert parse_pack_size("1/2 BBL").unit == "gal" and parse_pack_size("1/2 BBL").is_single_container
    assert parse_pack_size("1/6 BBL").base_units_per_case == Decimal(31) / 6
    qty, price = _apply_pack_size(parse_pack_size("1/2 BBL"), Decimal("1"), Decimal("155.00"), "KEG", _product(BaseUom.gal))
    assert (qty, price) == (Decimal("15.5"), Decimal("10.0000"))
    with pytest.raises(PackSizeParseError):
        parse_pack_size("2/1 BBL")


def test_a_gross_is_not_grams():
    with pytest.raises(PackSizeParseError):
        parse_pack_size("1/10 GR")


def test_a_case_of_quarts_written_into_the_description_is_read():
    described = pack_from_description("CREAM HVY 40% 12/1 QT", "CS")
    assert described is not None and (described.unit, described.base_units_per_case) == ("gal", Decimal("3"))
    assert pack_from_description("PASTA PENNE 12/500 G", "CS").unit == "lb"
    # A size alone still isn't a case.
    assert pack_from_description("CREAM HVY 1 QT", "CS") is None


def test_a_bare_g_is_grams_only_where_it_cannot_be_gallons():
    """"4/1 G" is four gallon jugs on some invoices; never guessed."""
    assert parse_pack_size("24/85 G").unit == "lb"
    for ambiguous in ("4/1 G", "1/5 G", "5 G"):
        with pytest.raises(PackSizeParseError):
            parse_pack_size(ambiguous)
    assert pack_from_description("BLEACH 4/1 G", "CS") is None
    # And as a billing unit it's neither.
    assert normalize_price(None, Decimal("2"), Decimal("5.00"), "G", _product(BaseUom.gal)) == (None, None)
