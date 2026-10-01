"""The word that tells two products apart, misspellings, and what is offered
for a line that can't be priced. From a set of matching traps: 124 lines,
of which nine were offered a near-identical product and 29 got no suggestion
though the catalog had theirs."""
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.enums import ReviewStatus
from app.normalize import matcher
from app.normalize.description_expansion import correct_spelling, normalize_for_embedding, vocabulary_of
from app.normalize.distinguishing import conflicts, first_not_contradicted


@pytest.mark.parametrize(
    "line, product",
    [
        ("CHEESE MOZZARELLA BLOCK", "Mozzarella Shredded Whole Milk"),  # form
        ("EGG MEDIUM 15DZ", "Eggs Large"),  # size, in words
        ("EGGS X LARGE 15DZ", "Eggs Large"),
        ("CUPS HOT PAPER 12 OZ", "Cups 16oz Hot"),  # size, measured
        ("CUPS COLD PET 16 OZ", "Cups 16oz Hot"),  # hot and cold
        ("FOIL ROLL 18 INCH", "Foil Roll 12in"),
        ("BEEF GRND 81/19", "Ground Beef 80/20"),  # a ratio
        ("SHRIMP 21/25 P&D", "Shrimp 16/20 Peeled Deveined"),
        ("SHRIMP 21/25 CT RAW", "Shrimp 16/20 Peeled Deveined"),
        ("PEPPER BLACK WHOLE", "Black Pepper Ground"),
        ("OIL OLIVE POMACE", "Olive Oil Pure"),  # grade
        ("MILK REDUCED FAT 2%", "Whole Milk"),
        ("BUTTER SOLID UNSLTD", "Salted Butter"),
        ("ONION YLLW JBO 50#", "Onion Red"),  # color
        ("CHKN THIGH BONE IN", "Chicken Thigh Boneless Skinless"),
        ("CHKN THIGH BNLS SKLS", "Chicken Breast Boneless Skinless"),  # cut
        ("GREEN BEANS FRESH", "Green Beans Frozen"),
    ],
)
def test_a_line_and_a_product_that_differ_on_one_point_are_not_the_same(line, product):
    assert conflicts(line, product)


@pytest.mark.parametrize(
    "line, product",
    [
        ("CHEESE CHEDDAR SOLID", "Cheddar Block White"),  # solid is block; no color on the line
        ("GLOVES NITRILE LARGE", "Gloves Nitrile"),  # a point only the line speaks to
        ("CHKN BRST FRZ BNLS", "Chicken Breast Boneless Skinless"),
        ("CHKN BRST BNLS SKLS 6OZ", "Chicken Breast Boneless Skinless"),  # a portion size
        ("CHEESE MOZZ SHRD 4/5 LB", "Mozzarella Shredded Whole Milk"),  # a pack, not a ratio
        ("BACON 14/18 SL", "Bacon Sliced"),
        ("CUP HOT PPR 16Z WHT", "Cups 16oz Hot"),
        ("BAGS CAN LINER 33GAL", "Trash Liner 33 Gallon"),
        ("MILK 2% GAL", "2% Milk"),
        ("MOZ SHRED WM LMPS", "Mozzarella Shredded Whole Milk"),  # says both; agrees on one
        ("BEEF GRND 4/10#", "Ground Beef 80/20"),  # a pack, not a ratio
        ("EGG LG WHT 15DZ LS", "Eggs Large"),
        ("RICE LONG GRN PARBOIL", "Rice White Long Grain"),  # GRN is grain here
    ],
)
def test_a_point_only_one_of_them_speaks_to_is_no_conflict(line, product):
    assert not conflicts(line, product)


def test_the_nearest_product_is_offered_unless_the_line_contradicts_it():
    assert first_not_contradicted("TO-GO CNTR 32OZ", ["To-Go Container 32oz", "To-Go Container 8oz"]) == 0
    assert first_not_contradicted("ANYTHING", []) is None


def test_past_a_contradicted_product_another_variety_of_it_is_offered():
    assert first_not_contradicted("TO-GO CNTR 32OZ", ["To-Go Container 16oz", "Souffle Cup 2oz", "To-Go Container 32oz"]) == 2
    assert first_not_contradicted("ONION YLLW JBO 50#", ["Onion White", "Onion Yellow"]) == 1
    assert first_not_contradicted("MILK REDUCED FAT 2%", ["Whole Milk", "Buttermilk", "2% Milk"]) == 2


def test_past_a_contradicted_product_nothing_else_is_offered():
    """Past the shredded mozzarellas the nearest thing to a block of
    mozzarella was a block of cheddar; past both ground beefs, a beefsteak
    tomato; past both olive oils, vegetable oil."""
    cheeses = ["Mozzarella Shredded Whole Milk", "Cheddar Block White", "Mozzarella Shredded Part Skim", "Cream Cheese"]
    assert first_not_contradicted("CHEESE MOZZARELLA BLOCK", cheeses) is None
    beef = ["Ground Beef 90/10", "Ground Beef 80/20", "Tomato Beefsteak", "Beef Brisket", "Ground Turkey 85/15"]
    assert first_not_contradicted("BF GRD 81/19 FRZ", beef) is None
    oils = ["Olive Oil Pure", "Olive Oil Extra Virgin", "Vegetable Oil", "Canola Oil"]
    assert first_not_contradicted("OIL OLIVE POMACE", oils) is None
    cups = ["Cups 16oz Hot", "Cups 16oz Cold", "Souffle Cup 2oz", "Cup Lids", "To-Go Container 16oz"]
    assert first_not_contradicted("CUPS HOT PAPER 12 OZ", cups) is None
    assert first_not_contradicted("EGG MEDIUM 15DZ", ["Eggs Large", "Buttermilk", "Whole Chicken"]) is None
    # A variety has to say what the line says on the point at issue: turkey
    # breast says nothing about lean and fat.
    assert first_not_contradicted("GROUND TURKEY 93/7", ["Ground Turkey 85/15", "Turkey Breast Boneless"]) is None


def test_a_product_with_no_word_of_the_lines_name_is_not_looked_at():
    """"CHICKEN BREAST" is nearer Duck Breast than Chicken Breast Boneless
    Skinless."""
    assert first_not_contradicted("CHICKEN BRST", ["Duck Breast", "Chicken Breast Boneless Skinless"]) == 1
    assert first_not_contradicted("EGG LARGE 15DZ", ["Eggs Large"]) == 0  # EGG and EGGS
    assert first_not_contradicted("CUCU 24CT", ["Cucumber"]) == 0  # cut off at the column's width
    assert first_not_contradicted("GOLDCREST 02318", ["Chicken Breast Boneless Skinless"]) is None


VOCABULARY = vocabulary_of(
    ["Chicken Breast Boneless Skinless", "Mozzarella Shredded Whole Milk", "Avocado", "Salted Butter", "Mustard Yellow",
     "Potato Russet", "Jalapeno", "Zucchini"]  # fmt: skip
)


@pytest.mark.parametrize(
    "printed, corrected",
    [
        ("CHIKEN BRST", "CHICKEN BRST"),  # a letter missing
        ("MOZARELLA", "MOZZARELLA"),
        ("AVACADO", "AVOCADO"),  # a letter wrong
        ("ZUCCHINNI", "ZUCCHINI"),  # a letter too many
        ("JALAPENOS 10 LB", "JALAPENO 10 LB"),  # a plural is one letter too many, and harmless to drop
        ("CHCIKEN THIGH", "CHICKEN THIGH"),  # two letters swapped
        ("JALAPEÑO", "JALAPENO"),
        ("BATTER PANCAKE", "BATTER PANCAKE"),  # short words are left: batter is not butter
        ("CUSTARD", "CUSTARD"),  # not mustard: the first letter differs
        ("CHICKEN", "CHICKEN"),
        ("POTATOE RUSSET", "POTATOE RUSSET"),  # "POTATO" is under the length corrected to
    ],
)
def test_a_misspelled_word_is_put_right_only_when_it_is_one_slip_from_a_catalog_word(printed, corrected):
    assert correct_spelling(printed, VOCABULARY) == corrected


def test_a_percent_sign_and_accents_survive_into_what_is_searched_for():
    assert normalize_for_embedding("MILK REDUCED FAT 2%") == "MILK REDUCED FAT 2%"
    assert normalize_for_embedding("JALAPEÑO PEPPER FRESH") == "JALAPENO PEPPER FRESH"


@pytest.mark.parametrize(
    "printed, expected",
    [
        ("CHX BST B/S 6Z IQF", "CHICKEN BREAST BONELESS SKINLESS 6OZ FROZEN"),
        ("MOZ SHRED WM LMPS", "MOZZARELLA SHREDDED WHOLE MILK LOW MOISTURE PART SKIM"),
        ("TOM RMA 25", "TOMATO ROMA 25"),
        ("RST BF CHK", "ROAST BEEF CHUCK"),
        ("CHX THGH", "CHICKEN THIGH"),
        ("TUNA CHK LT", "TUNA CHK LT"),  # chunk, not chicken: CHK is spelled out only after beef
        ("EGG LG GRD AA", "EGG LARGE GRADE AA"),
        ("BF GRD 80/20", "BEEF GROUND 80 20"),
        ("PECHUGA DE POLLO S/H", "BREAST CHICKEN BONELESS"),
        ("ACEITE DE CANOLA", "OIL CANOLA"),
        ("HUEVOS GRANDES 15DZ", "EGGS LARGE 15DZ"),
        ("QUESO MOZZARELLA RALLADO", "CHEESE MOZZARELLA SHREDDED"),
        ("PICO DE GALLO", "PICO DE GALLO"),  # "DE" goes only with a word from the Spanish list
    ],
)
def test_terse_shorthand_and_kitchen_spanish_are_spelled_out(printed, expected):
    assert normalize_for_embedding(printed) == expected


# --- What is offered for a line that can't be priced ---------------------------


def _line(db, pack: str | None, description: str = "KETCHUP TOMATO FANCY"):
    return matcher.match_line_item(
        db, distributor_id=uuid.uuid4(), raw_sku=None, raw_description=description, raw_pack_size=pack,
        quantity=Decimal("2"), unit_price=Decimal("41.32"), uom="CS", tenant_id=uuid.uuid4(),
    )  # fmt: skip


def _nearest(monkeypatch, in_any_unit, in_the_packs_units=(None, None)):
    """Stands in for the similarity search: what it finds among products in
    any unit, and among those in the pack's own."""
    searched = []

    def search(db, raw_description, compatible_uoms, density_uoms=frozenset()):
        searched.append(compatible_uoms)
        return in_any_unit if compatible_uoms is None else in_the_packs_units

    monkeypatch.setattr(matcher, "match_by_embedding", search)
    return searched


def test_a_line_whose_pack_cant_be_read_is_still_told_what_it_probably_is(db_session, monkeypatch):
    """Every line of an invoice with no pack column got no suggestion at all."""
    ketchup = SimpleNamespace(id=uuid.uuid4(), name="Ketchup")
    _nearest(monkeypatch, (ketchup, Decimal("0.83")))
    for pack in ("#10", None, "2000/CS"):
        result = _line(db_session, pack)
        assert result.canonical_sku_id == ketchup.id and result.match_confidence == Decimal("0.83")
        # A suggestion and no more: no price, so it can't resolve by itself.
        assert result.review_status == ReviewStatus.pending and result.method == "unparseable_pack_size"
        assert (result.normalized_unit_price, result.normalized_qty_base, result.base_uom) == (None, None, None)


def test_however_alike_it_is_never_resolved_without_a_price(db_session, monkeypatch):
    ketchup = SimpleNamespace(id=uuid.uuid4(), name="Ketchup")
    _nearest(monkeypatch, (ketchup, Decimal("0.99")))
    assert _line(db_session, "#10").review_status == ReviewStatus.pending


def test_with_no_units_to_narrow_the_search_a_product_has_to_be_nearer_to_be_offered(db_session, monkeypatch):
    """Among products in any unit, "ROMAINE HEADS" was nearest Tomato Roma, at 0.61."""
    tomato = SimpleNamespace(id=uuid.uuid4(), name="Tomato Roma")
    _nearest(monkeypatch, (tomato, matcher.MIN_SIMILARITY_IN_ANY_UNIT - Decimal("0.01")))
    result = _line(db_session, None, "ROMAINE HEADS")
    assert result.canonical_sku_id is None and result.match_confidence is None
    _nearest(monkeypatch, (None, None))
    assert _line(db_session, None, "GOLDCREST 02318").canonical_sku_id is None


def test_a_product_priced_in_a_unit_the_pack_doesnt_count_in_is_named_and_not_priced(db_session, monkeypatch):
    """A case of 24 bottles counts bottles; Hot Sauce is priced by the fluid ounce."""
    hot_sauce = SimpleNamespace(id=uuid.uuid4(), name="Hot Sauce")
    searched = _nearest(monkeypatch, (hot_sauce, Decimal("0.75")))
    result = _line(db_session, "24/1 CT", "HOT SAUCE LOUISIANA")
    assert len(searched) == 2 and searched[1] is None
    assert result.canonical_sku_id == hot_sauce.id and result.review_status == ReviewStatus.pending
    assert result.normalized_unit_price is None and result.method == "embedding_review"


def test_a_line_with_nothing_near_it_in_any_unit_is_still_priced_for_a_new_product(db_session, monkeypatch):
    _nearest(monkeypatch, (None, None))
    result = _line(db_session, "4/5 LB", "SOMETHING NEW")
    assert result.canonical_sku_id is None and result.method == "new_candidate"
    assert result.normalized_unit_price == Decimal("2.066000")
