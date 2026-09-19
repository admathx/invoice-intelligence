"""Playwright e2e fixture (Phase 5 gate): creates a small, deterministic
review-queue scenario without spending Anthropic budget on real extraction —
same "ground truth stands in for extraction" reasoning as
seed_corpus_pipeline.py, just for two hand-built line items instead of the
whole corpus.

Subcommands (see backend/scripts/e2e_fixture.py --help via argparse):
  setup <tenant_id>          creates 2 pending review-queue line items, prints
                             their ids and identifying fields as JSON.
  setup-bulk <tenant_id> <n> creates n pending items that each already have a
                             suggested canonical_sku_id (the review-band
                             case) — for timing "clear N items via keyboard."
  verify-alias <distributor_id> <raw_sku> <raw_description>
                             calls the real matcher again for that
                             (distributor, raw_sku) pair and prints the
                             MatchResult as JSON — this is what "the next
                             matching line auto-resolves" means functionally.
"""
import argparse
import json
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy  # noqa: E402

from app.db import SessionLocal, bind_tenant  # noqa: E402
from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceObservation, SkuAlias  # noqa: E402
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus  # noqa: E402
from app.normalize.matcher import match_line_item  # noqa: E402


def _cleanup_prior_fixtures(db) -> None:
    """Each run creates a fresh, uuid-tagged distributor for isolation —
    without this, repeated `npx playwright test` runs (e.g. in CI) would
    accumulate one throwaway distributor/invoice per run forever.
    """
    prior_ids = [row[0] for row in db.execute(sqlalchemy.select(Distributor.id).where(Distributor.slug.like("e2e-%"))).all()]
    if not prior_ids:
        return
    invoice_ids = [
        row[0] for row in db.execute(sqlalchemy.select(Invoice.id).where(Invoice.distributor_id.in_(prior_ids))).all()
    ]
    line_item_ids = sqlalchemy.select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id.in_(invoice_ids))
    db.execute(sqlalchemy.delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_item_ids)))
    db.execute(sqlalchemy.delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id.in_(invoice_ids)))
    db.execute(sqlalchemy.delete(Invoice).where(Invoice.id.in_(invoice_ids)))
    db.execute(sqlalchemy.delete(SkuAlias).where(SkuAlias.distributor_id.in_(prior_ids)))
    db.execute(sqlalchemy.delete(Distributor).where(Distributor.id.in_(prior_ids)))
    db.commit()


def cmd_setup(tenant_id: str) -> None:
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db)

    tag = uuid.uuid4().hex[:8]
    distributor = Distributor(id=uuid.uuid4(), name=f"E2E Distributor {tag}", slug=f"e2e-test-{tag}")
    db.add(distributor)

    avocado = db.scalar(sqlalchemy.select(CanonicalSku).where(CanonicalSku.name == "Avocado"))

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor.id,
        invoice_number=f"E2E-{tag}",
        # A fixed, early date (not None): the review queue orders by
        # invoice_date ascending, and Postgres sorts NULLs last — an
        # unset date would put this fixture's items behind any real,
        # already-pending corpus review items instead of always first.
        invoice_date=date(2020, 1, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    db.add(invoice)

    confirm_sku = f"E2E-CONFIRM-{tag}"
    confirm_item = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        invoice_id=invoice.id,
        line_number=1,
        raw_description=f"E2E Confirm Test Item {tag}",
        raw_sku=confirm_sku,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("10.00"),
        extended_price=Decimal("10.00"),
        uom="EA",
        canonical_sku_id=avocado.id if avocado else None,
        match_confidence=Decimal("0.85"),
        review_status=ReviewStatus.pending,
    )
    db.add(confirm_item)

    correct_sku = f"E2E-CORRECT-{tag}"
    correct_item = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        invoice_id=invoice.id,
        line_number=2,
        raw_description=f"E2E Correct Test Item {tag}",
        raw_sku=correct_sku,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("5.00"),
        extended_price=Decimal("5.00"),
        uom="EA",
        canonical_sku_id=None,
        review_status=ReviewStatus.pending,
    )
    db.add(correct_item)
    db.commit()

    print(
        json.dumps(
            {
                "distributor_id": str(distributor.id),
                "confirm_line_id": str(confirm_item.id),
                "confirm_raw_description": confirm_item.raw_description,
                "correct_line_id": str(correct_item.id),
                "correct_raw_sku": correct_sku,
                "correct_raw_description": correct_item.raw_description,
            }
        )
    )


def cmd_setup_bulk(tenant_id: str, count: int) -> None:
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db)

    tag = uuid.uuid4().hex[:8]
    distributor = Distributor(id=uuid.uuid4(), name=f"E2E Bulk Distributor {tag}", slug=f"e2e-bulk-{tag}")
    db.add(distributor)

    # Oldest-first: the real ~200-item catalog was seeded in Phase 1, before
    # any test run could have created a stray CanonicalSku row of its own —
    # picking newest-first (or unordered) risks a match against test debris
    # instead of a real catalog item.
    catalog = list(db.scalars(sqlalchemy.select(CanonicalSku).order_by(CanonicalSku.created_at).limit(count)))
    if len(catalog) < count:
        raise SystemExit(f"catalog has only {len(catalog)} SKUs, need {count}")

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor.id,
        invoice_number=f"E2E-BULK-{tag}",
        invoice_date=date(2020, 1, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    db.add(invoice)

    line_ids = []
    for i, sku in enumerate(catalog):
        line = InvoiceLineItem(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            invoice_id=invoice.id,
            line_number=i + 1,
            raw_description=f"E2E Bulk Item {tag} #{i}",
            raw_sku=f"E2E-BULK-{tag}-{i}",
            raw_pack_size="1 EA",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            extended_price=Decimal("10.00"),
            uom="EA",
            canonical_sku_id=sku.id,
            match_confidence=Decimal("0.85"),
            review_status=ReviewStatus.pending,
        )
        db.add(line)
        line_ids.append(str(line.id))
    db.commit()

    print(json.dumps({"distributor_id": str(distributor.id), "invoice_date": "2020-01-01", "line_ids": line_ids}))


def cmd_verify_alias(distributor_id: str, raw_sku: str, raw_description: str) -> None:
    db = SessionLocal()
    result = match_line_item(
        db,
        distributor_id=uuid.UUID(distributor_id),
        raw_sku=raw_sku,
        raw_description=raw_description,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("5.00"),
        uom="EA",
    )
    print(
        json.dumps(
            {
                "method": result.method,
                "review_status": result.review_status.value,
                "canonical_sku_id": str(result.canonical_sku_id) if result.canonical_sku_id else None,
                "match_confidence": str(result.match_confidence) if result.match_confidence else None,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setup_p = sub.add_parser("setup")
    setup_p.add_argument("tenant_id")
    bulk_p = sub.add_parser("setup-bulk")
    bulk_p.add_argument("tenant_id")
    bulk_p.add_argument("count", type=int)
    verify_p = sub.add_parser("verify-alias")
    verify_p.add_argument("distributor_id")
    verify_p.add_argument("raw_sku")
    verify_p.add_argument("raw_description")

    args = parser.parse_args()
    if args.command == "setup":
        cmd_setup(args.tenant_id)
    elif args.command == "setup-bulk":
        cmd_setup_bulk(args.tenant_id, args.count)
    elif args.command == "verify-alias":
        cmd_verify_alias(args.distributor_id, args.raw_sku, args.raw_description)


if __name__ == "__main__":
    main()
