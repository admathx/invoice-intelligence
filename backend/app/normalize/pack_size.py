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


class PackSizeParseError(ValueError):
    """The pack size string doesn't match any known format. Never guess a
    number here — an unparseable pack size must surface as a gap, not a
    silently wrong benchmark.
    """


@dataclass(frozen=True)
class ParsedPackSize:
    unit: str  # "lb" | "oz" | "gal" | "dz" | "ea"
    base_units_per_case: Decimal

    @property
    def compatible_base_uoms(self) -> set[BaseUom]:
        return _TOKEN_TO_BASE_UOMS[self.unit]


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
        return _positive(ParsedPackSize(unit="oz", base_units_per_case=Decimal(count) * CAN_SIZE_OZ[can_code]), raw_pack_size)

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


def _positive(parsed: ParsedPackSize, raw_pack_size: str) -> ParsedPackSize:
    # A zero (or negative, though the grammar can't produce one) case size
    # would divide-by-zero downstream in matcher.py's price normalization —
    # reject it here as unparseable rather than letting that propagate as an
    # uncaught crash that fails the whole invoice.
    if parsed.base_units_per_case <= 0:
        raise PackSizeParseError(f"non-positive pack size in {raw_pack_size!r}: {parsed.base_units_per_case}")
    return parsed
