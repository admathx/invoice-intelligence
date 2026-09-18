"""Market price model for the synthetic corpus.

Every canonical SKU gets a stable "market baseline" price per base unit, seeded
deterministically from its name (not from the shared corpus RNG) so the baseline
doesn't shift depending on generation order. On top of that baseline:

  price = market_baseline(sku, week)
        * tenant_distributor_markup(tenant, distributor)
        * creep_multiplier(tenant, sku, week)   [only for injected-creep pairs]
        * spot_buy_multiplier                    [only on the rare spot-buy line]

All randomness is driven by a `random.Random` the caller owns, so the whole
corpus is reproducible from one master seed.
"""
import math
import random
from dataclasses import dataclass
from decimal import Decimal

from synthetic.money import apply_ratio, q

WEEKS_PER_YEAR = 52

# (low, high) dollars per base unit, and seasonal amplitude as a fraction of
# baseline (produce and proteins swing with the season; paper goods and
# cleaning chemicals don't).
#
# Keyed by (category, base_uom), not category alone: dollars-per-base-unit means
# something very different for a gallon of oil than for a fluid ounce of hot
# sauce, and several categories mix base UOMs (e.g. "oils" holds both bulk
# cooking oil by the gallon and condiments by the fluid ounce).
#
# Amplitudes are kept deliberately small relative to Phase 4's creep threshold
# (>5% move over a 4-week-vs-8-week-prior window, per thresholds.yaml). A sine
# wave of amplitude A over a 52-week cycle can drift by roughly A * 1.45 across
# a 12-week window at its steepest point; keeping A well under ~0.03-0.04 for
# every category leaves that worst case under 5%, so seasonality alone
# shouldn't trip Phase 4's "zero false positives on stable SKUs" gate. If it
# still does, that's a Phase 4 finding to fix then (e.g. detrend by category),
# not a reason to have quietly loosened the threshold here.
CATEGORY_UOM_PRICE_PROFILE: dict[tuple[str, str], tuple[float, float, float]] = {
    # (category, base_uom): (price_low, price_high, seasonal_amplitude)
    ("proteins", "lb"): (2.50, 16.00, 0.025),
    ("dairy", "lb"): (2.00, 9.00, 0.015),
    ("dairy", "gal"): (2.50, 6.00, 0.015),
    ("dairy", "dozen"): (1.50, 4.00, 0.02),
    ("produce", "lb"): (0.40, 4.50, 0.035),
    ("produce", "each"): (0.25, 1.50, 0.035),
    ("oils", "gal"): (3.00, 22.00, 0.02),
    ("oils", "fl_oz"): (0.05, 0.35, 0.02),
    ("oils", "lb"): (0.50, 4.00, 0.01),
    ("flour", "lb"): (0.60, 3.50, 0.01),
    ("flour", "each"): (0.15, 1.00, 0.01),
    ("paper", "each"): (0.04, 0.60, 0.005),
    ("cleaning", "gal"): (2.50, 16.00, 0.005),
    ("cleaning", "fl_oz"): (0.03, 0.25, 0.005),
    ("cleaning", "each"): (0.20, 5.00, 0.005),
    ("cleaning", "lb"): (1.00, 5.00, 0.005),
}


@dataclass(frozen=True)
class PriceProfile:
    base_price: Decimal
    seasonal_amplitude: float
    seasonal_phase: float  # radians


def build_price_profiles(catalog_items: list[tuple[str, str, str]]) -> dict[str, PriceProfile]:
    """One PriceProfile per canonical SKU name, seeded from the name itself.

    `catalog_items` is (name, category, base_uom) triples.
    """
    profiles: dict[str, PriceProfile] = {}
    for name, category, base_uom in catalog_items:
        rng = random.Random(f"price::{name}")
        low, high, amplitude = CATEGORY_UOM_PRICE_PROFILE[(category, base_uom)]
        # The one place a price is created from scratch rather than derived from
        # an existing Decimal — going through str() avoids binary-float noise
        # leaking into the quantized result.
        base_price = q(Decimal(str(rng.uniform(low, high))))
        phase = rng.uniform(0, 2 * math.pi)
        profiles[name] = PriceProfile(base_price=base_price, seasonal_amplitude=amplitude, seasonal_phase=phase)
    return profiles


def market_baseline(profile: PriceProfile, week: int, rng: random.Random) -> Decimal:
    """Seasonal commodity baseline for a given ISO-ish week index (0..N), plus small noise."""
    seasonal = 1 + profile.seasonal_amplitude * math.sin(2 * math.pi * week / WEEKS_PER_YEAR + profile.seasonal_phase)
    noise = 1 + rng.uniform(-0.008, 0.008)
    return apply_ratio(profile.base_price, seasonal * noise)


def tenant_distributor_markup(rng: random.Random) -> Decimal:
    """Drawn once per (tenant, distributor) pair: how much that distributor marks this tenant up."""
    markup = max(1.03, rng.gauss(1.18, 0.06))
    return q(Decimal(str(markup)))


def creep_multiplier(weeks_since_creep_start: int, creep_duration_weeks: int, target_pct: float) -> float:
    """Linear ramp from 1.0 to (1 + target_pct) over creep_duration_weeks, then holds."""
    if weeks_since_creep_start <= 0:
        return 1.0
    progress = min(1.0, weeks_since_creep_start / creep_duration_weeks)
    return 1.0 + target_pct * progress
