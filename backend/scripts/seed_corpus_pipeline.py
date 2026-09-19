"""Phase 4 prerequisite: runs the full synthetic corpus through Invoice /
InvoiceLineItem creation and the real matcher, populating price_observations
at corpus scale so app/analytics has real data to analyze.

Ground truth line items are treated as already-extracted (Phase 2's vision
extraction is validated separately, at real cost, via `make extraction-report`
— this script exists so Phase 4's gates don't also require spending Anthropic
budget on 520 real invoices). What differs from a real run is only the
extraction step; every invoice still goes through the real pack-size/alias/
embedding matcher exactly as app/workers/tasks.py's process_invoice does.

Dedupes the expensive part (embedding lookups) by (raw_description,
raw_pack_size, uom): the corpus's descriptions are stable per (distributor
layout, canonical SKU) across all 26 weeks (~978 distinct triples for 39,179
line items, per validation/matching_report.py's own count), so this is
seconds of embedding work, not the ~70 minutes a naive per-line call would
cost. Only canonical_sku_id/base_uom/review_status/match_confidence are
cached this way; normalized_qty_base/normalized_unit_price are recomputed per
line via the real parse_pack_size + matcher._apply_pack_size, since those
depend on that line's own quantity/unit_price, which varies week to week.

Idempotent: clears any previously-seeded invoices for these 20 tenants before
reinserting, so reruns after a normalization or pricing change don't
duplicate rows. Run via `make seed-analytics`.
"""
import json
import sys
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

from app.db import SessionLocal, bind_tenant  # noqa: E402
from app.models import Distributor, Invoice, InvoiceLineItem, PriceObservation, Tenant  # noqa: E402
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus, VolumeTier  # noqa: E402
from app.normalize.matcher import MatchResult, _apply_pack_size, match_line_item  # noqa: E402
from app.normalize.pack_size import PackSizeParseError, parse_pack_size  # noqa: E402
from synthetic.tenants import build_tenants  # noqa: E402

OUT_DIR = REPO_ROOT / "synthetic" / "out"


def _get_or_create_tenant(db, synth_tenant) -> Tenant:
    existing = db.scalar(select(Tenant).where(Tenant.name == synth_tenant.name))
    if existing is not None:
        return existing
    tenant = Tenant(
        id=uuid.uuid4(),
        name=synth_tenant.name,
        metro=synth_tenant.metro,
        volume_tier=VolumeTier(synth_tenant.volume_tier),
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _clear_existing_corpus_data(db, tenant_id: uuid.UUID) -> None:
    invoice_ids = [row[0] for row in db.execute(select(Invoice.id).where(Invoice.tenant_id == tenant_id)).all()]
    if not invoice_ids:
        return
    line_item_ids_subq = select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id.in_(invoice_ids))
    db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_item_ids_subq)))
    db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id.in_(invoice_ids)))
    db.execute(delete(Invoice).where(Invoice.id.in_(invoice_ids)))
    db.commit()


def main() -> None:
    db = SessionLocal()
    distributor_ids = {d.slug: d.id for d in db.scalars(select(Distributor))}
    synth_tenants = build_tenants()

    match_cache: dict[tuple[str, str, str], MatchResult] = {}
    total_invoices = total_lines = total_observations = 0
    t0 = time.time()

    for synth_tenant in synth_tenants:
        tenant = _get_or_create_tenant(db, synth_tenant)
        bind_tenant(db, tenant.id)
        _clear_existing_corpus_data(db, tenant.id)

        tenant_dir = OUT_DIR / f"tenant_{synth_tenant.index:02d}"
        week_files = sorted(tenant_dir.glob("week_*.json"))
        for week_file in week_files:
            gt = json.loads(week_file.read_text())
            distributor_id = distributor_ids[gt["distributor"]]
            invoice_date = datetime.strptime(gt["invoice_date"], "%Y-%m-%d").date()
            delivery_date = datetime.strptime(gt["delivery_date"], "%Y-%m-%d").date() if gt.get("delivery_date") else None

            invoice_id = uuid.uuid4()
            db.add(
                Invoice(
                    id=invoice_id,
                    tenant_id=tenant.id,
                    distributor_id=distributor_id,
                    invoice_number=gt["invoice_number"],
                    invoice_date=invoice_date,
                    delivery_date=delivery_date,
                    subtotal=Decimal(gt["subtotal"]),
                    tax=Decimal(gt["tax"]),
                    total=Decimal(gt["total"]),
                    source=InvoiceSource.upload,
                    original_file_uri=f"file://{week_file.with_suffix('.pdf').resolve()}",
                    status=InvoiceStatus.extracted,
                    extraction_model="ground_truth",
                    extraction_cost_usd=Decimal("0"),
                )
            )

            for line in gt["line_items"]:
                key = (line["raw_description"], line["raw_pack_size"] or "", line["uom"])
                cached = match_cache.get(key)
                if cached is None:
                    cached = match_line_item(
                        db,
                        distributor_id=distributor_id,
                        raw_sku=line["raw_sku"],
                        raw_description=line["raw_description"],
                        raw_pack_size=line["raw_pack_size"],
                        quantity=Decimal("1"),
                        unit_price=Decimal("1"),
                        uom=line["uom"],
                    )
                    match_cache[key] = cached

                quantity = Decimal(line["quantity"])
                unit_price = Decimal(line["unit_price"])
                if cached.normalized_qty_base is not None:
                    # Same pack string as the cache-priming call, so this can't
                    # raise where the cached call didn't — recomputed (not
                    # reused) because qty/price vary per line.
                    pack = parse_pack_size(line["raw_pack_size"])
                    qty_base, price_base = _apply_pack_size(pack, quantity, unit_price, line["uom"])
                else:
                    qty_base = price_base = None

                line_item_id = uuid.uuid4()
                db.add(
                    InvoiceLineItem(
                        id=line_item_id,
                        tenant_id=tenant.id,
                        invoice_id=invoice_id,
                        line_number=line["line_number"],
                        raw_description=line["raw_description"],
                        raw_sku=line["raw_sku"],
                        raw_pack_size=line["raw_pack_size"],
                        quantity=quantity,
                        unit_price=unit_price,
                        extended_price=Decimal(line["extended_price"]),
                        uom=line["uom"],
                        canonical_sku_id=cached.canonical_sku_id,
                        normalized_qty_base=qty_base,
                        normalized_unit_price=price_base,
                        base_uom=cached.base_uom,
                        extraction_confidence=Decimal(str(line["confidence"])),
                        match_confidence=cached.match_confidence,
                        review_status=cached.review_status,
                    )
                )
                total_lines += 1

                if cached.review_status == ReviewStatus.auto and cached.canonical_sku_id is not None:
                    db.add(
                        PriceObservation(
                            id=uuid.uuid4(),
                            tenant_id=tenant.id,
                            canonical_sku_id=cached.canonical_sku_id,
                            distributor_id=distributor_id,
                            observed_on=invoice_date,
                            unit_price_base=price_base,
                            metro=tenant.metro,
                            volume_tier=tenant.volume_tier,
                            invoice_line_item_id=line_item_id,
                        )
                    )
                    total_observations += 1

            total_invoices += 1

        db.commit()
        print(f"  {synth_tenant.name}: {len(week_files)} invoices seeded ({time.time()-t0:.0f}s elapsed)")

    print(
        f"\nSeeded {total_invoices} invoices, {total_lines} line items, {total_observations} price observations "
        f"in {time.time()-t0:.1f}s ({len(match_cache)} distinct match computations)."
    )


if __name__ == "__main__":
    main()
