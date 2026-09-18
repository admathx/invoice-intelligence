from synthetic.templates.base import DistributorLayout

GORDON = DistributorLayout(
    slug="gordon",
    display_name="Gordon Food Service",
    address_lines=("333 Berry St SW", "Wyoming, MI 49548"),
    # Description leads on Gordon invoices, unlike the other three.
    column_order=("description", "sku", "pack_size", "uom", "qty", "unit_price", "ext_price"),
    column_labels={
        "description": "Description",
        "sku": "Item Code",
        "pack_size": "Pack",
        "uom": "U/M",
        "qty": "Qty",
        "unit_price": "Price/Unit",
        "ext_price": "Total",
    },
    abbreviation_intensity=0.3,
    sku_digits=5,
    sku_prefix="GF-",
)
