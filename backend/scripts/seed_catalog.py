"""Idempotently seed the canonical SKU catalog (rows + description embeddings)
into the DB. Part of `make seed`.
"""
from app.db import SessionLocal
from app.normalize.catalog import CANONICAL_SKUS, backfill_canonical_embeddings, seed_canonical_skus

if __name__ == "__main__":
    db = SessionLocal()
    try:
        name_to_id = seed_canonical_skus(db)
        print(f"Seeded {len(CANONICAL_SKUS)} canonical SKUs ({len(name_to_id)} now in DB).")
        backfilled = backfill_canonical_embeddings(db)
        print(f"Backfilled description embeddings for {backfilled} canonical SKUs.")
    finally:
        db.close()
