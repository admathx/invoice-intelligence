"""Realistic case pack configurations per base UOM, in the same "N/size UNIT" style
SPEC.md §6 gives as pack-size-parser examples (e.g. "4/5 LB", "2/1 GAL").

Phase 1 doesn't parse these back (that's Phase 3's pack_size.py) — it just needs to
generate them internally-consistently: raw_pack_size is a human-readable string,
and base_units_per_case is the ground-truth number that string encodes, used here
to price the case (unit_price = base price * base_units_per_case) so extended =
quantity * unit_price still reconciles.
"""
import random
from dataclasses import dataclass

from app.models.enums import BaseUom

# (units_per_case, size_per_unit, unit_label) options per base UOM.
_PACK_OPTIONS: dict[BaseUom, list[tuple[int, float, str]]] = {
    BaseUom.lb: [(4, 5, "LB"), (6, 5, "LB"), (1, 10, "LB"), (1, 25, "LB"), (1, 40, "LB"), (2, 10, "LB")],
    BaseUom.gal: [(4, 1, "GAL"), (2, 1, "GAL"), (6, 1, "GAL")],
    BaseUom.fl_oz: [(12, 16, "OZ"), (24, 12, "OZ"), (6, 32, "OZ"), (12, 8, "OZ")],
    BaseUom.each: [(100, 1, "CT"), (50, 1, "CT"), (6, 12, "CT"), (12, 1, "CT"), (250, 1, "CT")],
    BaseUom.dozen: [(15, 1, "DZ"), (10, 1, "DZ")],
    BaseUom.oz: [(24, 8, "OZ"), (12, 16, "OZ")],
}


@dataclass(frozen=True)
class PackConfig:
    raw_pack_size: str
    base_units_per_case: float
    case_uom: str  # what's printed in the invoice's UOM column, e.g. "CS"


def random_pack_config(base_uom: BaseUom, rng: random.Random) -> PackConfig:
    units, size, label = rng.choice(_PACK_OPTIONS[base_uom])
    base_units_per_case = units * size
    if units == 1:
        raw_pack_size = f"{size:g} {label}"
    else:
        raw_pack_size = f"{units}/{size:g} {label}"
    return PackConfig(raw_pack_size=raw_pack_size, base_units_per_case=base_units_per_case, case_uom="CS")
