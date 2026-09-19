"""The canonical SKU catalog: the ~200 items that cover most of a restaurant's spend.

Per SPEC.md §6: proteins, dairy, produce staples, oils, flour, paper goods, cleaning.
Do not attempt the full catalog — this is deliberately curated, not exhaustive.

Also imported by synthetic/generate.py so the synthetic corpus's ground truth refers
to the same canonical items the real normalization pipeline will eventually match
against. `name` is the stable identifier used for that cross-reference (there's no
separate slug column on canonical_skus in the schema, and these names are unique).
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.canonical_sku import CanonicalSku
from app.models.enums import BaseUom


@dataclass(frozen=True)
class CatalogItem:
    name: str
    category: str
    subcategory: str
    base_uom: BaseUom
    manufacturer: str | None = None


_lb = BaseUom.lb
_oz = BaseUom.oz
_gal = BaseUom.gal
_fl_oz = BaseUom.fl_oz
_each = BaseUom.each
_dozen = BaseUom.dozen

CANONICAL_SKUS: list[CatalogItem] = [
    # --- Proteins (~40) ---
    CatalogItem("Chicken Breast Boneless Skinless", "proteins", "poultry", _lb),
    CatalogItem("Chicken Thigh Boneless Skinless", "proteins", "poultry", _lb),
    CatalogItem("Chicken Thigh Bone-In", "proteins", "poultry", _lb),
    CatalogItem("Chicken Wings Bone-In", "proteins", "poultry", _lb),
    CatalogItem("Chicken Tenders", "proteins", "poultry", _lb),
    CatalogItem("Chicken Drumsticks", "proteins", "poultry", _lb),
    CatalogItem("Whole Chicken", "proteins", "poultry", _lb),
    CatalogItem("Turkey Breast Boneless", "proteins", "poultry", _lb),
    CatalogItem("Ground Turkey 85/15", "proteins", "poultry", _lb),
    CatalogItem("Duck Breast", "proteins", "poultry", _lb),
    CatalogItem("Ground Beef 80/20", "proteins", "beef", _lb),
    CatalogItem("Ground Beef 90/10", "proteins", "beef", _lb),
    CatalogItem("Ribeye Steak", "proteins", "beef", _lb),
    CatalogItem("Sirloin Steak", "proteins", "beef", _lb),
    CatalogItem("Beef Brisket", "proteins", "beef", _lb),
    CatalogItem("Beef Short Rib", "proteins", "beef", _lb),
    CatalogItem("Beef Tenderloin", "proteins", "beef", _lb),
    CatalogItem("Beef Chuck Roast", "proteins", "beef", _lb),
    CatalogItem("Beef Flank Steak", "proteins", "beef", _lb),
    CatalogItem("Beef Skirt Steak", "proteins", "beef", _lb),
    CatalogItem("Pork Shoulder", "proteins", "pork", _lb),
    CatalogItem("Pork Chop Boneless", "proteins", "pork", _lb),
    CatalogItem("Pork Tenderloin", "proteins", "pork", _lb),
    CatalogItem("Pork Belly", "proteins", "pork", _lb),
    CatalogItem("Bacon Sliced", "proteins", "pork", _lb),
    CatalogItem("Sausage Links Italian", "proteins", "pork", _lb),
    CatalogItem("Sausage Links Breakfast", "proteins", "pork", _lb),
    CatalogItem("Ham Deli Sliced", "proteins", "deli", _lb),
    CatalogItem("Turkey Deli Sliced", "proteins", "deli", _lb),
    CatalogItem("Salami Deli Sliced", "proteins", "deli", _lb),
    CatalogItem("Roast Beef Deli Sliced", "proteins", "deli", _lb),
    CatalogItem("Salmon Fillet", "proteins", "seafood", _lb),
    CatalogItem("Shrimp 16/20 Peeled Deveined", "proteins", "seafood", _lb),
    CatalogItem("Shrimp 21/25 Peeled Deveined", "proteins", "seafood", _lb),
    CatalogItem("Tilapia Fillet", "proteins", "seafood", _lb),
    CatalogItem("Cod Fillet", "proteins", "seafood", _lb),
    CatalogItem("Scallops Sea", "proteins", "seafood", _lb),
    CatalogItem("Crab Meat Lump", "proteins", "seafood", _lb),
    CatalogItem("Mussels", "proteins", "seafood", _lb),
    CatalogItem("Lamb Chop", "proteins", "lamb", _lb),
    CatalogItem("Lamb Leg Boneless", "proteins", "lamb", _lb),
    # --- Dairy (~26) ---
    CatalogItem("Mozzarella Shredded Whole Milk", "dairy", "cheese", _lb),
    CatalogItem("Mozzarella Shredded Part Skim", "dairy", "cheese", _lb),
    CatalogItem("Cheddar Shredded Yellow", "dairy", "cheese", _lb),
    CatalogItem("Cheddar Block White", "dairy", "cheese", _lb),
    CatalogItem("Parmesan Grated", "dairy", "cheese", _lb),
    CatalogItem("Feta Crumbled", "dairy", "cheese", _lb),
    CatalogItem("Provolone Sliced", "dairy", "cheese", _lb),
    CatalogItem("Swiss Sliced", "dairy", "cheese", _lb),
    CatalogItem("American Sliced", "dairy", "cheese", _lb),
    CatalogItem("Ricotta Whole Milk", "dairy", "cheese", _lb),
    CatalogItem("Gouda Sliced", "dairy", "cheese", _lb),
    CatalogItem("Brie Wheel", "dairy", "cheese", _lb),
    CatalogItem("Blue Cheese Crumbled", "dairy", "cheese", _lb),
    CatalogItem("Mascarpone", "dairy", "cheese", _lb),
    CatalogItem("Cream Cheese", "dairy", "cheese", _lb),
    CatalogItem("Cottage Cheese", "dairy", "cheese", _lb),
    CatalogItem("Sour Cream", "dairy", "cultured", _lb),
    CatalogItem("Yogurt Plain", "dairy", "cultured", _lb),
    CatalogItem("Buttermilk", "dairy", "milk", _gal),
    CatalogItem("Heavy Cream", "dairy", "milk", _gal),
    CatalogItem("Half And Half", "dairy", "milk", _gal),
    CatalogItem("Whole Milk", "dairy", "milk", _gal),
    CatalogItem("2% Milk", "dairy", "milk", _gal),
    CatalogItem("Unsalted Butter", "dairy", "butter", _lb),
    CatalogItem("Salted Butter", "dairy", "butter", _lb),
    CatalogItem("Eggs Large", "dairy", "eggs", _dozen),
    # --- Produce (~40) ---
    CatalogItem("Tomato Roma", "produce", "vegetables", _lb),
    CatalogItem("Tomato Beefsteak", "produce", "vegetables", _lb),
    CatalogItem("Tomato Cherry", "produce", "vegetables", _lb),
    CatalogItem("Lettuce Iceberg", "produce", "vegetables", _lb),
    CatalogItem("Lettuce Romaine", "produce", "vegetables", _lb),
    CatalogItem("Onion Yellow", "produce", "vegetables", _lb),
    CatalogItem("Onion Red", "produce", "vegetables", _lb),
    CatalogItem("Onion White", "produce", "vegetables", _lb),
    CatalogItem("Bell Pepper Green", "produce", "vegetables", _lb),
    CatalogItem("Bell Pepper Red", "produce", "vegetables", _lb),
    CatalogItem("Jalapeno", "produce", "vegetables", _lb),
    CatalogItem("Garlic Peeled", "produce", "vegetables", _lb),
    CatalogItem("Potato Russet", "produce", "vegetables", _lb),
    CatalogItem("Potato Sweet", "produce", "vegetables", _lb),
    CatalogItem("Carrot", "produce", "vegetables", _lb),
    CatalogItem("Celery", "produce", "vegetables", _lb),
    CatalogItem("Cucumber", "produce", "vegetables", _lb),
    CatalogItem("Avocado", "produce", "vegetables", _each),
    CatalogItem("Lime", "produce", "vegetables", _each),
    CatalogItem("Lemon", "produce", "vegetables", _each),
    CatalogItem("Cilantro", "produce", "herbs", _each),
    CatalogItem("Parsley", "produce", "herbs", _each),
    CatalogItem("Basil", "produce", "herbs", _each),
    CatalogItem("Spinach", "produce", "vegetables", _lb),
    CatalogItem("Mushroom Button", "produce", "vegetables", _lb),
    CatalogItem("Mushroom Cremini", "produce", "vegetables", _lb),
    CatalogItem("Broccoli Crown", "produce", "vegetables", _lb),
    CatalogItem("Cauliflower", "produce", "vegetables", _lb),
    CatalogItem("Cabbage Green", "produce", "vegetables", _lb),
    CatalogItem("Corn Kernels Frozen", "produce", "frozen", _lb),
    CatalogItem("Green Beans Frozen", "produce", "frozen", _lb),
    CatalogItem("Mixed Berries Frozen", "produce", "frozen", _lb),
    CatalogItem("Banana", "produce", "fruit", _lb),
    CatalogItem("Apple", "produce", "fruit", _lb),
    CatalogItem("Orange", "produce", "fruit", _lb),
    CatalogItem("Zucchini", "produce", "vegetables", _lb),
    CatalogItem("Yellow Squash", "produce", "vegetables", _lb),
    CatalogItem("Arugula", "produce", "vegetables", _lb),
    CatalogItem("Kale", "produce", "vegetables", _lb),
    CatalogItem("Scallion", "produce", "vegetables", _each),
    CatalogItem("Ginger Root", "produce", "vegetables", _lb),
    # --- Oils & condiments (~24) ---
    CatalogItem("Canola Oil", "oils", "cooking_oil", _gal),
    CatalogItem("Olive Oil Extra Virgin", "oils", "cooking_oil", _gal),
    CatalogItem("Olive Oil Pure", "oils", "cooking_oil", _gal),
    CatalogItem("Vegetable Oil", "oils", "cooking_oil", _gal),
    CatalogItem("Peanut Oil", "oils", "cooking_oil", _gal),
    CatalogItem("Sesame Oil", "oils", "cooking_oil", _fl_oz),
    CatalogItem("Ketchup", "oils", "condiments", _gal),
    CatalogItem("Mayonnaise", "oils", "condiments", _gal),
    CatalogItem("Mustard Yellow", "oils", "condiments", _gal),
    CatalogItem("Mustard Dijon", "oils", "condiments", _fl_oz),
    CatalogItem("Hot Sauce", "oils", "condiments", _fl_oz),
    CatalogItem("Soy Sauce", "oils", "condiments", _gal),
    CatalogItem("Worcestershire Sauce", "oils", "condiments", _fl_oz),
    CatalogItem("BBQ Sauce", "oils", "condiments", _gal),
    CatalogItem("Ranch Dressing", "oils", "condiments", _gal),
    CatalogItem("Italian Dressing", "oils", "condiments", _gal),
    CatalogItem("Balsamic Vinegar", "oils", "condiments", _fl_oz),
    CatalogItem("Red Wine Vinegar", "oils", "condiments", _fl_oz),
    CatalogItem("Honey", "oils", "condiments", _lb),
    CatalogItem("Maple Syrup", "oils", "condiments", _fl_oz),
    CatalogItem("Sugar Granulated", "oils", "baking", _lb),
    CatalogItem("Sugar Brown", "oils", "baking", _lb),
    CatalogItem("Salt Kosher", "oils", "seasoning", _lb),
    CatalogItem("Black Pepper Ground", "oils", "seasoning", _lb),
    # --- Flour & dry goods (~24) ---
    CatalogItem("All Purpose Flour", "flour", "baking", _lb),
    CatalogItem("Bread Flour", "flour", "baking", _lb),
    CatalogItem("Cornmeal", "flour", "baking", _lb),
    CatalogItem("Cornstarch", "flour", "baking", _lb),
    CatalogItem("Rice White Long Grain", "flour", "grains", _lb),
    CatalogItem("Rice Brown", "flour", "grains", _lb),
    CatalogItem("Pasta Spaghetti", "flour", "pasta", _lb),
    CatalogItem("Pasta Penne", "flour", "pasta", _lb),
    CatalogItem("Panko Breadcrumbs", "flour", "baking", _lb),
    CatalogItem("Baking Powder", "flour", "baking", _lb),
    CatalogItem("Baking Soda", "flour", "baking", _lb),
    CatalogItem("Yeast Active Dry", "flour", "baking", _lb),
    CatalogItem("Black Beans Dried", "flour", "legumes", _lb),
    CatalogItem("Pinto Beans Dried", "flour", "legumes", _lb),
    CatalogItem("Chickpeas Dried", "flour", "legumes", _lb),
    CatalogItem("Quinoa", "flour", "grains", _lb),
    CatalogItem("Oats Rolled", "flour", "grains", _lb),
    CatalogItem("Tortilla Flour", "flour", "bread", _each),
    CatalogItem("Tortilla Corn", "flour", "bread", _each),
    CatalogItem("Pizza Dough Frozen", "flour", "bread", _each),
    CatalogItem("Bread Hamburger Bun", "flour", "bread", _each),
    CatalogItem("Bread Hoagie Roll", "flour", "bread", _each),
    CatalogItem("Croutons", "flour", "baking", _lb),
    CatalogItem("Cocoa Powder", "flour", "baking", _lb),
    # --- Paper goods (~24) ---
    CatalogItem("Napkins Dinner", "paper", "napkins", _each),
    CatalogItem("Napkins Cocktail", "paper", "napkins", _each),
    CatalogItem("Paper Towel Roll", "paper", "towels", _each),
    CatalogItem("To-Go Container 8oz", "paper", "containers", _each),
    CatalogItem("To-Go Container 16oz", "paper", "containers", _each),
    CatalogItem("To-Go Container 32oz", "paper", "containers", _each),
    CatalogItem("Foil Wrap", "paper", "wrap", _each),
    CatalogItem("Plastic Wrap", "paper", "wrap", _each),
    CatalogItem("Deli Paper Sheets", "paper", "wrap", _each),
    CatalogItem("Straws Wrapped", "paper", "disposables", _each),
    CatalogItem("Cups 16oz Hot", "paper", "cups", _each),
    CatalogItem("Cups 16oz Cold", "paper", "cups", _each),
    CatalogItem("Cup Lids", "paper", "cups", _each),
    CatalogItem("Take-Out Bags Paper", "paper", "bags", _each),
    CatalogItem("Take-Out Bags Plastic", "paper", "bags", _each),
    CatalogItem("Gloves Nitrile", "paper", "disposables", _each),
    CatalogItem("Apron Disposable", "paper", "disposables", _each),
    CatalogItem("Trash Liner 33 Gallon", "paper", "disposables", _each),
    CatalogItem("Parchment Paper Roll", "paper", "wrap", _each),
    CatalogItem("Pizza Box 14in", "paper", "containers", _each),
    CatalogItem("Pizza Box 16in", "paper", "containers", _each),
    CatalogItem("Souffle Cup 2oz", "paper", "containers", _each),
    CatalogItem("Wax Paper Sheets", "paper", "wrap", _each),
    CatalogItem("Chafing Fuel", "paper", "disposables", _each),
    # --- Cleaning (~24) ---
    CatalogItem("Dish Soap", "cleaning", "chemicals", _gal),
    CatalogItem("Degreaser Spray", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Sanitizer Solution", "cleaning", "chemicals", _gal),
    CatalogItem("Bleach", "cleaning", "chemicals", _gal),
    CatalogItem("Floor Cleaner", "cleaning", "chemicals", _gal),
    CatalogItem("Glass Cleaner", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Hand Soap", "cleaning", "chemicals", _gal),
    CatalogItem("Hand Sanitizer", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Oven Cleaner", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Drain Cleaner", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Scouring Pad", "cleaning", "tools", _each),
    CatalogItem("Sponge", "cleaning", "tools", _each),
    CatalogItem("Mop Head", "cleaning", "tools", _each),
    CatalogItem("Broom", "cleaning", "tools", _each),
    CatalogItem("Dust Pan", "cleaning", "tools", _each),
    CatalogItem("Trash Can Liner Heavy Duty", "cleaning", "disposables", _each),
    CatalogItem("Quaternary Sanitizer Tablets", "cleaning", "chemicals", _each),
    CatalogItem("Stainless Steel Cleaner", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Laundry Detergent", "cleaning", "chemicals", _gal),
    CatalogItem("Fryer Boil-Out Cleaner", "cleaning", "chemicals", _lb),
    CatalogItem("Air Freshener", "cleaning", "chemicals", _fl_oz),
    CatalogItem("Rubber Gloves", "cleaning", "tools", _each),
    CatalogItem("Bar Mop Towel", "cleaning", "tools", _each),
    CatalogItem("Wiping Cloth", "cleaning", "tools", _each),
]


def seed_canonical_skus(db: Session) -> dict[str, uuid.UUID]:
    """Idempotent: inserts any catalog item missing by name, returns name -> id for all of them."""
    existing = {row.name: row.id for row in db.scalars(select(CanonicalSku))}
    for item in CANONICAL_SKUS:
        if item.name in existing:
            continue
        row = CanonicalSku(
            id=uuid.uuid4(),
            name=item.name,
            category=item.category,
            subcategory=item.subcategory,
            base_uom=item.base_uom,
            manufacturer=item.manufacturer,
        )
        db.add(row)
        existing[item.name] = row.id
    db.commit()
    return existing


def backfill_canonical_embeddings(db: Session) -> int:
    """Idempotent: computes description_embedding for any canonical SKU that
    doesn't have one yet (Phase 3's embedding matcher needs it on every row it
    searches). Returns the number backfilled. Canonical names are already
    full-word (not abbreviated like raw invoice text), so no expansion needed
    before embedding — see app/normalize/description_expansion.py for that.
    """
    from app.normalize.embeddings import embed_text  # local import: avoid loading the model for callers that only seed rows

    rows = list(db.scalars(select(CanonicalSku).where(CanonicalSku.description_embedding.is_(None))))
    for row in rows:
        row.description_embedding = embed_text(row.name)
    db.commit()
    return len(rows)
