from dataclasses import dataclass

from synthetic.abbreviate import abbreviate_description
from synthetic.pack_sizes import PackConfig, random_pack_config
from synthetic.rng import rng_for


@dataclass(frozen=True)
class DistributorLayout:
    slug: str
    display_name: str
    address_lines: tuple[str, str]
    # Column order as rendered on the invoice; keys index into the per-line-item dict
    # built in synthetic/generate.py (sku, description, pack_size, qty, uom, unit_price, ext_price).
    column_order: tuple[str, ...]
    column_labels: dict[str, str]
    abbreviation_intensity: float
    sku_digits: int
    sku_prefix: str

    def sku_code(self, canonical_name: str) -> str:
        rng = rng_for("sku", self.slug, canonical_name)
        number = rng.randint(0, 10**self.sku_digits - 1)
        return f"{self.sku_prefix}{number:0{self.sku_digits}d}"

    def description_for(self, canonical_name: str) -> str:
        rng = rng_for("descr", self.slug, canonical_name)
        return abbreviate_description(canonical_name, self.abbreviation_intensity, rng)

    def pack_config_for(self, canonical_name: str, base_uom) -> PackConfig:
        rng = rng_for("pack", self.slug, canonical_name)
        return random_pack_config(base_uom, rng)
