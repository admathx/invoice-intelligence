"""The unit a line is billed in, when the model's answer can't be used as is.

Most invoices print no unit column, and the model then fills the field
anyway: on the first set of realistic test invoices it copied the pack size
("4/1 GAL", "4/10 LB") into it on 807 of 1,924 lines, and not even
consistently (the same Sysco layout came back "CS" one week and pack sizes
the next). A pack size isn't a unit, so none of those lines could be priced
per pound or per case, matched, or tracked.
"""
from decimal import Decimal


def _squash(text: str | None) -> str:
    return "".join((text or "").upper().split())


def billing_unit(uom: str | None, raw_pack_size: str | None, quantity: Decimal) -> str:
    """The unit as printed when it is one (CS, EA, LB, GAL...). When the field
    is empty or holds a pack size instead: by the pound for a line whose
    quantity is a weight (38.46, a catch weight), by the case otherwise,
    which is how almost everything on a distributor invoice is sold."""
    printed = (uom or "").strip().upper()
    looks_like_pack = (
        not printed
        or any(c.isdigit() for c in printed)
        or "/" in printed
        or (raw_pack_size and _squash(printed) == _squash(raw_pack_size))
    )
    if not looks_like_pack:
        return printed
    return "LB" if quantity != quantity.to_integral_value() else "CS"
