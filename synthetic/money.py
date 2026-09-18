"""The one place money gets rounded in the synthetic corpus.

SPEC.md §11: "Decimal everywhere for money... Never floats. Ever." Ratios and
multipliers (seasonal factors, markups, noise) are inherently float — they come
from math.sin/random.uniform/random.gauss and aren't money themselves — but the
moment a ratio is applied to a Decimal price, that step must happen in Decimal,
and rounding must happen exactly once per value, via this function, not via two
differently-configured helpers scattered across the package.
"""
from decimal import ROUND_HALF_UP, Decimal


def q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def apply_ratio(price: Decimal, ratio: float) -> Decimal:
    """price * ratio, rounded once. `ratio` is a dimensionless multiplier, not money."""
    return q(price * Decimal(str(ratio)))
