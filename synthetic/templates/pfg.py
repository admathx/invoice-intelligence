from synthetic.templates.base import DistributorLayout

PFG = DistributorLayout(
    slug="pfg",
    display_name="Performance Food Group",
    address_lines=("12500 West Creek Pkwy", "Richmond, VA 23238"),
    column_order=("line_number", "sku", "description", "pack_size", "qty", "uom", "unit_price", "ext_price"),
    column_labels={
        "line_number": "Ln",
        "sku": "Item#",
        "description": "Description",
        "pack_size": "Pack/Size",
        "qty": "Qty Shipped",
        "uom": "U/M",
        "unit_price": "Unit Price",
        "ext_price": "Extended",
    },
    abbreviation_intensity=0.65,
    sku_digits=6,
    sku_prefix="P",
)
