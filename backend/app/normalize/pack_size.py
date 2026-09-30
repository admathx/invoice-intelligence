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
from dataclasses import dataclass
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


def billed_unit_token(uom: str) -> str | None:
    """The physical unit a line's billing UOM names, or None if it names none
    (a container such as BG, BX or PK, whose size the invoice doesn't state)."""
    return _UNIT_TO_TOKEN.get(uom.strip().upper())


@dataclass(frozen=True)
class ParsedPackSize:
    unit: str  # "lb" | "oz" | "gal" | "dz" | "ea"
    base_units_per_case: Decimal
    # Can codes give net weight ("#10" is 110 oz of product, not of liquid),
    # so a can's ounces are never read as fluid ounces.
    net_weight: bool = False

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


def parse_pack_size(raw_pack_size: str | None) -> ParsedPackSize:
    if not raw_pack_size:
        raise PackSizeParseError(f"empty or missing pack size: {raw_pack_size!r}")
    text = raw_pack_size.strip().upper()

    m = _CAN_PATTERN.match(text)
    if m:
        count, can_code = m.group(1), m.group(2)
        if can_code not in CAN_SIZE_OZ:
            raise PackSizeParseError(f"unknown can size #{can_code} in {raw_pack_size!r}")
        return _positive(
            ParsedPackSize(unit="oz", base_units_per_case=Decimal(count) * CAN_SIZE_OZ[can_code], net_weight=True), raw_pack_size
        )

    m = _CASE_PATTERN.match(text)
    if m:
        count, size, unit_raw = m.groups()
        unit = _UNIT_TO_TOKEN.get(unit_raw)
        if unit is None:
            raise PackSizeParseError(f"unknown unit {unit_raw!r} in {raw_pack_size!r}")
        return _positive(ParsedPackSize(unit=unit, base_units_per_case=Decimal(count) * Decimal(size)), raw_pack_size)

    m = _BARE_PATTERN.match(text)
    if m:
        size, unit_raw = m.groups()
        unit = _UNIT_TO_TOKEN.get(unit_raw)
        if unit is None:
            raise PackSizeParseError(f"unknown unit {unit_raw!r} in {raw_pack_size!r}")
        return _positive(ParsedPackSize(unit=unit, base_units_per_case=Decimal(size)), raw_pack_size)

    raise PackSizeParseError(f"unrecognized pack size format: {raw_pack_size!r}")


def pack_for_line(raw_pack_size: str | None, uom: str) -> ParsedPackSize:
    """The pack a line's price is for. As printed, or, when no pack is
    printed and the line is billed by weight or volume ("LB", "GAL"), one of
    that unit: salmon at $9.70 billed LB is $9.70 a pound, and treating the
    missing pack as unreadable left such lines with no price and no
    suggestion. A count ("EA") or a case ("CS") with no pack still says
    nothing about the amount, so those stay unreadable."""
    if not (raw_pack_size or "").strip():
        unit = billed_unit_token(uom)
        if unit in ("lb", "oz", "gal"):
            return ParsedPackSize(unit=unit, base_units_per_case=Decimal(1))
    return parse_pack_size(raw_pack_size)


def _positive(parsed: ParsedPackSize, raw_pack_size: str) -> ParsedPackSize:
    # A zero (or negative, though the grammar can't produce one) case size
    # would divide-by-zero downstream in matcher.py's price normalization —
    # reject it here as unparseable rather than letting that propagate as an
    # uncaught crash that fails the whole invoice.
    if parsed.base_units_per_case <= 0:
        raise PackSizeParseError(f"non-positive pack size in {raw_pack_size!r}: {parsed.base_units_per_case}")
    return parsed
