from synthetic.templates.base import DistributorLayout

US_FOODS = DistributorLayout(
    slug="us_foods",
    display_name="US Foods, Inc.",
    address_lines=("9399 W Higgins Rd", "Rosemont, IL 60018"),
    column_order=("sku", "description", "pack_size", "uom", "qty", "unit_price", "ext_price"),
    column_labels={
        "sku": "Product Code",
        "description": "Description",
        "pack_size": "Pack Size",
        "uom": "Unit",
        "qty": "Qty Ordered",
        "unit_price": "Price",
        "ext_price": "Amount",
    },
    abbreviation_intensity=0.5,
    sku_digits=6,
    sku_prefix="",
)
