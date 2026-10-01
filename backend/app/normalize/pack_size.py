"""SPEC.md §6: "the single highest-leverage piece of code in the repo" — a
pack-size error produces a confidently wrong benchmark, which is worse than no
benchmark. Pure function, no DB/network dependency.

Parses strings like "4/5 LB" -> 4 units x 5 lb = 20 lb, "6/#10 CAN" -> 6 x 110 oz
= 660 oz, "2/1 GAL" -> 2 gal. Handles CS/EA/DZ/#10-style can codes, and
fractional per-unit weights via decimal notation ("4/2.5 LB" -> 10 lb).

The parsed unit is a physical unit token, not directly a BaseUom: a bare "OZ"
on an invoice is genuinely ambiguous between weight ounces (most canned/dry
goods) and fluid ounces (oils, liquid cleaners) — the pack string alone can't
resolve that. compatible_base_uoms() below maps the parsed unit to the set of
BaseUom values it could mean, and the caller (matcher.py) narrows using the
candidate canonical SKU's own declared base_uom.
"""
import re
from dataclasses import dataclass, replace
from decimal import Decimal

from app.models.enums import BaseUom

# Standard can sizes, net weight in oz. SPEC.md §6 gives #10 -> 110 oz as its
# own worked example; the others are common foodservice can codes added for
# adversarial test coverage, not exercised by the synthetic corpus.
CAN_SIZE_OZ: dict[str, Decimal] = {
    "10": Decimal("110"),
    "5": Decimal("56"),
    "2.5": Decimal("28"),
    "303": Decimal("16"),
}

_UNIT_TO_TOKEN = {
    "LB": "lb",
    "LBS": "lb",
    "OZ": "oz",
    "GAL": "gal",
    "DZ": "dz",
    "DOZ": "dz",
    "CT": "ea",
    "EA": "ea",
    "EACH": "ea",
}

# Units written another way, read into the pounds and gallons everything
# else is compared in: a "12/1 QT" case is 3 gallons, a "12/500 G" case
# 13.2 lb. Dairy comes in quarts and pints, imported goods in metric, and
# none of them could be priced per pound or gallon before. FLOZ is "FL OZ"
# (parse_pack_size joins it): unlike a bare OZ, never weight. Not GR, which
# is as often a gross.
_SCALED_UNITS: dict[str, tuple[str, Decimal]] = {
    "QT": ("gal", Decimal("0.25")),
    "QTS": ("gal", Decimal("0.25")),
    "PT": ("gal", Decimal("0.125")),
    "PTS": ("gal", Decimal("0.125")),
    "FLOZ": ("gal", Decimal(1) / 128),
    "L": ("gal", Decimal("0.264172")),
    "LT": ("gal", Decimal("0.264172")),
    "LTR": ("gal", Decimal("0.264172")),
    "ML": ("gal", Decimal("0.000264172")),
    "KG": ("lb", Decimal("2.20462")),
    "KGS": ("lb", Decimal("2.20462")),
    "GM": ("lb", Decimal("0.00220462")),
}
# A bare G is grams in "12/500 G" and gallons in "4/1 G", which distributors
# also write. Grams from this size up; below it, unreadable rather than
# guessed. Only in a pack size: as a billing unit it's never taken as either.
_GRAMS = ("lb", Decimal("0.00220462"))
_MIN_SIZE_IN_GRAMS = Decimal(20)

# A keg is a fraction of a 31 gallon barrel: "1/2 BBL" is 15.5 gallons, not
# one container of two.
_BARREL_GALLONS = Decimal(31)
_BARREL_UNITS = {"BBL", "BARREL", "KEG"}

_TOKEN_TO_BASE_UOMS: dict[str, set[BaseUom]] = {
    "lb": {BaseUom.lb},
    "oz": {BaseUom.oz, BaseUom.fl_oz},  # genuinely ambiguous — see module docstring
    "gal": {BaseUom.gal},
    "dz": {BaseUom.dozen},
    "ea": {BaseUom.each},
}


# How many of a product's base unit one unit of a pack holds, for the
# conversions that are exact whatever the product: weight to weight, volume
# to volume, count to count. The catalog prices each product in one unit
# (Bleach per gallon, Bar Mop Towel each) while distributors pack them in
# others ("6/121 OZ", "12 DZ"); without these, a product was only ever found
# for a pack in its own unit. Weight to volume needs the product's density,
# so it isn't here: a 35 lb jug of oil doesn't compare with oil priced per
# gallon.
_BASE_UNITS_PER_PACK_UNIT: dict[str, dict[BaseUom, Decimal]] = {
    "lb": {BaseUom.lb: Decimal(1), BaseUom.oz: Decimal(16)},
    # A bare OZ is weight or fluid (module docstring); the product's own unit
    # says which.
    "oz": {BaseUom.oz: Decimal(1), BaseUom.fl_oz: Decimal(1), BaseUom.lb: Decimal(1) / 16, BaseUom.gal: Decimal(1) / 128},
    "gal": {BaseUom.gal: Decimal(1), BaseUom.fl_oz: Decimal(128)},
    "dz": {BaseUom.dozen: Decimal(1), BaseUom.each: Decimal(12)},
    "ea": {BaseUom.each: Decimal(1), BaseUom.dozen: Decimal(1) / 12},
}
_VOLUME = {BaseUom.fl_oz, BaseUom.gal}

# Through a product's own weight per gallon (CanonicalSku.lb_per_gal), for
# the few liquids sold both ways: a pack's pounds per pack unit, a pack's
# gallons per pack unit, and a product unit's count per pound or per gallon.
_POUNDS_PER_PACK_UNIT = {"lb": Decimal(1), "oz": Decimal(1) / 16}
_GALLONS_PER_PACK_UNIT = {"gal": Decimal(1)}
_PER_POUND = {BaseUom.lb: Decimal(1), BaseUom.oz: Decimal(16)}
_PER_GALLON = {BaseUom.gal: Decimal(1), BaseUom.fl_oz: Decimal(128)}


class PackSizeParseError(ValueError):
    """The pack size string doesn't match any known format. Never guess a
    number here — an unparseable pack size must surface as a gap, not a
    silently wrong benchmark.
    """


class BilledUnitMismatchError(PackSizeParseError):
    """The line is billed in a unit the pack size can't convert to its base unit.

    A subclass of PackSizeParseError on purpose: every caller already routes
    that to review instead of guessing, which is exactly the right handling
    here too. The pack string parsed fine; what can't be trusted is the price
    per base unit.
    """


class ProductUnitMismatchError(PackSizeParseError):
    """The pack's unit can't be converted to the unit the product is priced
    in (pounds against gallons). Also a PackSizeParseError, for the same
    reason as BilledUnitMismatchError: no price rather than a wrong one."""


# A pack's pieces: "6/3 PK" is six packs of three, 18 pieces. A roll's length
# counts rolls: "1/500 FT" is one roll, which is how the catalog prices foil
# and film. Not inches: "14 IN" is a size (a pizza box, a tortilla), and read
# as one roll it put a whole case's price on one box. Only in a pack size;
# as a billing unit, a PK is one whole pack.
_PACK_COUNT_UNITS = {"PK": "ea", "PACK": "ea", "FT": "ea", "FEET": "ea", "YD": "ea"}
_LENGTHS = {"FT", "FEET", "YD"}

# Billing units that are the whole case whatever the pack.
CASE_UNITS = {"CS", "CASE", "CA", "CTN", "CARTON"}
# Billing units naming one container: the whole pack when the pack is a
# single container ("50 LB" billed BG is one 50 lb bag), but ambiguous
# against a pack of several ("4/5 LB" billed BG: one bag, or the case?).
CONTAINER_UNITS = {
    "BG", "BAG", "BX", "BOX", "PK", "PKG", "PAIL", "PL", "JG", "JUG", "TB", "TUB",
    "BKT", "BUCKET", "CN", "CAN", "BTL", "BOTTLE", "SK", "SACK", "RL", "ROLL", "KEG",
}  # fmt: skip


def _unit(unit_raw: str) -> tuple[str | None, Decimal]:
    """A written unit as (the unit it's compared in, how many of those one
    is): LB is ("lb", 1), QT is ("gal", 0.25). (None, 1) if it names none."""
    if unit_raw in _SCALED_UNITS:
        return _SCALED_UNITS[unit_raw]
    return _UNIT_TO_TOKEN.get(unit_raw), Decimal(1)


def billed_unit_token(uom: str) -> str | None:
    """The physical unit a line's billing UOM names, or None if it names none
    (a container such as BG, BX or PK, whose size the invoice doesn't state)."""
    return _unit(_join_fl_oz(uom.strip().upper()))[0]


def billed_unit_scale(uom: str) -> Decimal:
    """How many of billed_unit_token's unit one billed unit is: a line
    billed per QT is a quarter gallon each."""
    return _unit(_join_fl_oz(uom.strip().upper()))[1]


def _join_fl_oz(text: str) -> str:
    return re.sub(r"\bFL\.?\s*OZ\b\.?", "FLOZ", text)


@dataclass(frozen=True)
class ParsedPackSize:
    unit: str  # "lb" | "oz" | "gal" | "dz" | "ea"
    base_units_per_case: Decimal
    # Can codes give net weight ("#10" is 110 oz of product, not of liquid),
    # so a can's ounces are never read as fluid ounces.
    net_weight: bool = False
    # How many containers the case holds ("4/5 LB": 4); None for a bare
    # size ("50 LB"), which is one.
    count: Decimal | None = None
    # Read from the item's description, not a printed pack (pack_for_line):
    # used to price a line only when it converts to the matched product.
    from_description: bool = False

    @property
    def is_single_container(self) -> bool:
        return self.count is None or self.count == 1

    @property
    def compatible_base_uoms(self) -> set[BaseUom]:
        """The units this pack is written in, with no conversion."""
        if self.net_weight:
            return {BaseUom.oz}
        return _TOKEN_TO_BASE_UOMS[self.unit]

    @property
    def convertible_base_uoms(self) -> set[BaseUom]:
        """Every unit a product could be priced in and still be compared."""
        return {uom for uom in _BASE_UNITS_PER_PACK_UNIT[self.unit] if not (self.net_weight and uom in _VOLUME)}

    @property
    def _is_weight(self) -> bool:
        # A bare OZ could be fluid; only pounds and can weights are surely weight.
        return self.unit == "lb" or (self.unit == "oz" and self.net_weight)

    @property
    def base_uoms_by_density(self) -> set[BaseUom]:
        """Units a product with a weight per gallon could be priced in,
        beyond convertible_base_uoms."""
        if self._is_weight:
            return set(_PER_GALLON)
        if self.unit in _GALLONS_PER_PACK_UNIT:
            return set(_PER_POUND)
        return set()

    def base_units_per_pack_unit(self, product_uom: BaseUom, lb_per_gal: Decimal | None = None) -> Decimal | None:
        """How many of `product_uom` one of this pack's units holds, or None
        when they can't be compared. `lb_per_gal`, the product's, allows
        weight against volume."""
        if product_uom in self.convertible_base_uoms:
            return _BASE_UNITS_PER_PACK_UNIT[self.unit][product_uom]
        if not lb_per_gal or lb_per_gal <= 0 or product_uom not in self.base_uoms_by_density:
            return None
        if self._is_weight:
            return _POUNDS_PER_PACK_UNIT[self.unit] / lb_per_gal * _PER_GALLON[product_uom]
        return _GALLONS_PER_PACK_UNIT[self.unit] * lb_per_gal * _PER_POUND[product_uom]


# The size groups are `\d+(?:\.\d+)?`, not `[\d.]+`: the looser form also
# matches nonsense like "5.5.5", which then reaches Decimal() and raises
# decimal.InvalidOperation — NOT a ValueError, so it sails past every
# `except PackSizeParseError` in the codebase and takes down the whole
# invoice instead of failing as one unparseable line.
_CAN_PATTERN = re.compile(r"^(\d+)\s*/\s*#\s*(\d+(?:\.\d+)?)\s*CAN$", re.IGNORECASE)
_CASE_PATTERN = re.compile(r"^(\d+)\s*/\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)$")
_BARE_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$")
_KEG_PATTERN = re.compile(r"^(\d+)\s*/\s*(\d+)\s*(?:%s)$" % "|".join(sorted(_BARREL_UNITS)))


def _pack_unit(unit_raw: str, size: str, raw_pack_size: str) -> tuple[str, Decimal]:
    """A pack size's unit as (the unit it's compared in, how many of those
    one is)."""
    if unit_raw == "G":
        if Decimal(size) < _MIN_SIZE_IN_GRAMS:
            raise PackSizeParseError(f"{raw_pack_size!r} could be grams or gallons")
        return _GRAMS
    unit, per_unit = _unit(unit_raw)
    unit = unit or _PACK_COUNT_UNITS.get(unit_raw)
    if unit is None:
        raise PackSizeParseError(f"unknown unit {unit_raw!r} in {raw_pack_size!r}")
    return unit, per_unit


def parse_pack_size(raw_pack_size: str | None) -> ParsedPackSize:
    if not raw_pack_size:
        raise PackSizeParseError(f"empty or missing pack size: {raw_pack_size!r}")
    text = raw_pack_size.strip().upper()
    # "50#" and "4/5#" are pounds. But "6/10#" is as likely six #10 cans (the
    # usual can) as six 10 lb bags, so that stays unreadable.
    counted_can = re.match(r"^\d+\s*/\s*(10)\s*#$", text)
    if counted_can:
        raise PackSizeParseError(f"{raw_pack_size!r} could be #{counted_can.group(1)} cans or pounds")
    text = _join_fl_oz(re.sub(r"(\d)\s*#$", r"\1 LB", text))

    m = _KEG_PATTERN.match(text)
    if m:
        part, whole = Decimal(m.group(1)), Decimal(m.group(2))
        if not 0 < part < whole:
            raise PackSizeParseError(f"{raw_pack_size!r} isn't a fraction of a barrel")
        return ParsedPackSize(unit="gal", base_units_per_case=_BARREL_GALLONS * part / whole)

    m = _CAN_PATTERN.match(text)
    if m:
        count, can_code = m.group(1), m.group(2)
        if can_code not in CAN_SIZE_OZ:
            raise PackSizeParseError(f"unknown can size #{can_code} in {raw_pack_size!r}")
        return _positive(
            ParsedPackSize(
                unit="oz", base_units_per_case=Decimal(count) * CAN_SIZE_OZ[can_code], net_weight=True, count=Decimal(count)
            ),
            raw_pack_size,
        )

    m = _CASE_PATTERN.match(text)
    if m:
        count, size, unit_raw = m.groups()
        if unit_raw in _LENGTHS:
            return _positive(ParsedPackSize(unit="ea", base_units_per_case=Decimal(count), count=Decimal(count)), raw_pack_size)
        unit, per_unit = _pack_unit(unit_raw, size, raw_pack_size)
        return _positive(
            ParsedPackSize(unit=unit, base_units_per_case=Decimal(count) * Decimal(size) * per_unit, count=Decimal(count)),
            raw_pack_size,
        )

    m = _BARE_PATTERN.match(text)
    if m:
        size, unit_raw = m.groups()
        if unit_raw in _LENGTHS:
            return ParsedPackSize(unit="ea", base_units_per_case=Decimal(1))
        unit, per_unit = _pack_unit(unit_raw, size, raw_pack_size)
        return _positive(ParsedPackSize(unit=unit, base_units_per_case=Decimal(size) * per_unit), raw_pack_size)

    raise PackSizeParseError(f"unrecognized pack size format: {raw_pack_size!r}")


# A pack written into an item's description, on invoices with no pack
# column. Deliberately narrow, because the description also carries sizes
# that aren't the case:
# - a count and a size ("BBQ SAUCE ORIGINAL 4/1 GAL", "CHEESE 4/5 LB"): the
#   case, as a pack column would print it;
# - but not "N/1 LB" on a patty or burger, where "4/1" means four to the
#   pound;
# - a bare size ("FLOUR ALL PURPOSE 50 LB") only on a line billed per bag,
#   sack or tub, where it is the thing billed. Billed per case, a bare size
#   is as often the unit inside ("SOUR CREAM 5 LB" in a case of four) and
#   priced a tub of sour cream at four times its price per pound;
# - never a bare OZ ("CHICKEN BREAST 6OZ", "CUPS 16 OZ"), a count ("120CT"
#   slices), or "6/10#" (six #10 cans, or six 10 lb bags).
_DESCRIBED_CASE = re.compile(
    r"(?<![\d/.])([2-9]|[1-9]\d+)\s*/\s*(\d+(?:\.\d+)?)\s*(LBS?|GAL|DZ|DOZ|OZ|QT|PT|KG|ML|L|G)(?![A-Z0-9])"
)
_DESCRIBED_BARE = re.compile(r"(?<![\d/.])(\d+(?:\.\d+)?)\s*(LBS?|#|GAL|DZ|DOZ)(?![A-Z0-9])")


_PER_POUND_COUNT = re.compile(r"\b(PATTY|PATTIES|PTY|BURGER|BURGERS|HAMBURGER)\b")
# Billed per one of these, a bare size in the description is the thing billed.
_SINGLE_CONTAINER_BILLING = {"BG", "BAG", "SK", "SACK", "TB", "TUB", "PAIL", "PL", "BKT", "BUCKET", "JG", "JUG"}


def pack_from_description(description: str | None, uom: str | None = None) -> ParsedPackSize | None:
    text = (description or "").upper()
    m = _DESCRIBED_CASE.search(text)
    if m and m.group(2) == "1" and m.group(3).startswith("LB") and _PER_POUND_COUNT.search(text):
        m = None
    raw = f"{m.group(1)}/{m.group(2)} {m.group(3)}" if m else None
    if raw is None and (uom or "").strip().upper() in _SINGLE_CONTAINER_BILLING:
        m = _DESCRIBED_BARE.search(text)
        raw = f"{m.group(1)} {'LB' if m.group(2) == '#' else m.group(2)}" if m else None
    if raw is None:
        return None
    try:
        return replace(parse_pack_size(raw), from_description=True)
    except PackSizeParseError:
        return None


def pack_for_line(raw_pack_size: str | None, uom: str, description: str | None = None) -> ParsedPackSize:
    """The pack a line's price is for. As printed; or, when none is printed:
    - billed by weight or volume ("LB", "GAL"), one of that unit: salmon at
      $9.70 billed LB is $9.70 a pound (also when a pack is printed but can't
      be read);
    - otherwise, a pack written into the description, if there's a clear
      one (pack_from_description), for pricing only against a product it
      converts to.
    A count or a case with neither still says nothing about the amount, so
    that stays unreadable."""
    unit = billed_unit_token(uom)
    # One billed unit: a pound, or for a line billed per KG, 2.2 of them.
    by_measure = (
        ParsedPackSize(unit=unit, base_units_per_case=billed_unit_scale(uom)) if unit in ("lb", "oz", "gal") else None
    )
    if not (raw_pack_size or "").strip():
        if by_measure is not None:
            return by_measure
        described = pack_from_description(description, uom)
        if described is not None:
            return described
    try:
        return parse_pack_size(raw_pack_size)
    except PackSizeParseError:
        # A pack that can't be read ("2/30 LB AVG", "CASE WTS 20.10 20.60")
        # on a line billed by the pound: the price is per pound all the same.
        # Catch-weight meat prints exactly these, and went unpriced.
        if by_measure is not None and (raw_pack_size or "").strip():
            return by_measure
        raise


def _positive(parsed: ParsedPackSize, raw_pack_size: str) -> ParsedPackSize:
    # A zero (or negative, though the grammar can't produce one) case size
    # would divide-by-zero downstream in matcher.py's price normalization —
    # reject it here as unparseable rather than letting that propagate as an
    # uncaught crash that fails the whole invoice.
    if parsed.base_units_per_case <= 0:
        raise PackSizeParseError(f"non-positive pack size in {raw_pack_size!r}: {parsed.base_units_per_case}")
    return parsed
