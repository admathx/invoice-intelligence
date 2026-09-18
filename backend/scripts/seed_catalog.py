"""Idempotently seed the canonical SKU catalog into the DB. Part of `make seed`."""
from app.db import SessionLocal
from app.normalize.catalog import CANONICAL_SKUS, seed_canonical_skus

if __name__ == "__main__":
    db = SessionLocal()
    try:
        name_to_id = seed_canonical_skus(db)
        print(f"Seeded {len(CANONICAL_SKUS)} canonical SKUs ({len(name_to_id)} now in DB).")
    finally:
        db.close()
