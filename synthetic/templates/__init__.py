from synthetic.templates.base import DistributorLayout
from synthetic.templates.gordon import GORDON
from synthetic.templates.pfg import PFG
from synthetic.templates.sysco import SYSCO
from synthetic.templates.us_foods import US_FOODS

LAYOUTS: dict[str, DistributorLayout] = {
    layout.slug: layout for layout in [SYSCO, US_FOODS, GORDON, PFG]
}

__all__ = ["DistributorLayout", "LAYOUTS"]
