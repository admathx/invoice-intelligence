from synthetic.templates.base import DistributorLayout

SYSCO = DistributorLayout(
    slug="sysco",
    display_name="Sysco Corporation",
    address_lines=("1390 Enclave Parkway", "Houston, TX 77077"),
    column_order=("sku", "description", "pack_size", "qty", "uom", "unit_price", "ext_price"),
    column_labels={
        "sku": "Item #",
        "description": "Description",
        "pack_size": "Pack",
        "qty": "Qty",
        "uom": "U/M",
        "unit_price": "Unit Price",
        "ext_price": "Ext Price",
    },
    abbreviation_intensity=0.85,
    sku_digits=7,
    sku_prefix="",
)
