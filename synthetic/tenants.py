"""The 20 synthetic tenants: 3 metros x 4 volume tiers, each tied to one primary
distributor so 5 tenants exercise each of the 4 distributor layouts.
"""
from dataclasses import dataclass

METROS = ["Austin, TX", "Columbus, OH", "Sacramento, CA"]
VOLUME_TIERS = ["under_500k", "500k_1m", "1m_3m", "over_3m"]
DISTRIBUTORS = ["sysco", "us_foods", "gordon", "pfg"]

TENANT_NAMES = [
    "The Copper Skillet",
    "Blue Oak Kitchen",
    "Riverside Trattoria",
    "The Salted Fig",
    "Magnolia Diner",
    "Harbor & Hearth",
    "The Rustic Spoon",
    "Cedar Table",
    "Golden Hour Cafe",
    "The Iron Griddle",
    "Wildflower Bistro",
    "Stonegate Grill",
    "The Tin Roof",
    "Maple & Vine",
    "Prairie Fire BBQ",
    "The Humble Pie",
    "Amber Lantern",
    "Millbrook Tavern",
    "The Corner Ladle",
    "Sunbelt Kitchen",
]


@dataclass(frozen=True)
class SyntheticTenant:
    index: int
    name: str
    metro: str
    volume_tier: str
    distributor_slug: str


def build_tenants() -> list[SyntheticTenant]:
    tenants = []
    for i in range(20):
        tenants.append(
            SyntheticTenant(
                index=i,
                name=TENANT_NAMES[i],
                metro=METROS[i % len(METROS)],
                volume_tier=VOLUME_TIERS[i % len(VOLUME_TIERS)],
                distributor_slug=DISTRIBUTORS[i % len(DISTRIBUTORS)],
            )
        )
    return tenants
