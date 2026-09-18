"""SPEC.md §8 / Phase 1: generates the synthetic invoice corpus.

20 tenants x 26 weekly invoices x 4 distributor layouts, with a paired ground-truth
JSON file per PDF, deliberate price creep on a known subset of (tenant, SKU) pairs,
occasional spot-buy prices, and deliberate extraction noise (scan skew/JPEG, missing
SKU column, truncated descriptions) on a subset of invoices.

Deterministic: every random draw is seeded from MASTER_SEED plus stable keys (tenant
index, week, canonical SKU name), so re-running this script produces byte-identical
PDFs and JSON. Run via `make seed` (from repo root: `PYTHONPATH=backend python -m
synthetic.generate`), or directly as a module the same way.
"""
import json
import random
import shutil
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.normalize.catalog import CANONICAL_SKUS
from synthetic.noise import apply_scan_noise
from synthetic.pdf_writer import render_invoice_pdf
from synthetic.pricing import PriceProfile, build_price_profiles, creep_multiplier, market_baseline, tenant_distributor_markup
from synthetic.rng import rng_for
from synthetic.templates import LAYOUTS
from synthetic.tenants import SyntheticTenant, build_tenants

MASTER_SEED = 42
WEEKS = 26
LINE_ITEMS_MIN, LINE_ITEMS_MAX = 30, 120
START_DATE = date(2026, 3, 2)  # a Monday
NOISY_FRACTION = 0.18
TRUNCATION_PROB_ON_NOISY_LINE = 0.05
SPOT_BUY_PROB = 0.015
SPOT_BUY_MULT_RANGE = (1.15, 1.45)
CREEP_SKUS_PER_TENANT = (3, 5)  # inclusive range
CREEP_START_WEEK = 14
CREEP_DURATION_WEEKS = 12
CREEP_TARGET_PCT_RANGE = (0.10, 0.28)
BASKET_SIZE_RANGE = (100, 160)
# Case quantity scales with volume tier so weekly invoice totals land in a
# roughly plausible range for that tier's stated annual purchase volume.
VOLUME_TIER_QTY_MULTIPLIER = {
    "under_500k": 0.35,
    "500k_1m": 0.65,
    "1m_3m": 2.0,
    "over_3m": 4.5,
}

OUT_DIR = Path(__file__).parent / "out"

_CATALOG_BY_NAME = {item.name: item for item in CANONICAL_SKUS}
_PRICE_PROFILES: dict[str, PriceProfile] = build_price_profiles(
    [(i.name, i.category, i.base_uom.value) for i in CANONICAL_SKUS]
)


def _q(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.0001"))


def _tenant_basket(tenant: SyntheticTenant) -> list[str]:
    rng = rng_for("basket", tenant.index)
    size = min(len(CANONICAL_SKUS), rng.randint(*BASKET_SIZE_RANGE))
    names = [item.name for item in CANONICAL_SKUS]
    rng.shuffle(names)
    return sorted(names[:size])


def _tenant_creep_skus(tenant: SyntheticTenant, basket: list[str]) -> list[dict]:
    rng = rng_for("creep", tenant.index)
    n = rng.randint(*CREEP_SKUS_PER_TENANT)
    chosen = rng.sample(basket, min(n, len(basket)))
    injections = []
    for name in chosen:
        target_pct = rng.uniform(*CREEP_TARGET_PCT_RANGE)
        injections.append(
            {
                "canonical_sku_name": name,
                "target_pct": round(target_pct, 4),
                "creep_start_week": CREEP_START_WEEK,
                "creep_duration_weeks": CREEP_DURATION_WEEKS,
            }
        )
    return injections


def _week_selection(tenant: SyntheticTenant, week: int, basket: list[str]) -> list[str]:
    rng = rng_for("week_selection", tenant.index, week)
    target = rng.randint(LINE_ITEMS_MIN, LINE_ITEMS_MAX)
    target = min(target, len(basket))
    return sorted(rng.sample(basket, target))


def _build_line(
    tenant: SyntheticTenant,
    layout,
    line_number: int,
    sku_name: str,
    week: int,
    markup: Decimal,
    creep_by_sku: dict[str, dict],
    omit_sku_column: bool,
    truncate: bool,
    line_rng: random.Random,
) -> tuple[dict, dict]:
    item = _CATALOG_BY_NAME[sku_name]
    profile = _PRICE_PROFILES[sku_name]

    price_rng = rng_for("price_noise", tenant.index, sku_name, week)
    base_unit_price = market_baseline(profile, week, price_rng)

    creep = creep_by_sku.get(sku_name)
    is_creep_injected = False
    if creep is not None:
        weeks_since = week - creep["creep_start_week"]
        mult = creep_multiplier(weeks_since, creep["creep_duration_weeks"], creep["target_pct"])
        if mult > 1.0:
            is_creep_injected = True
        base_unit_price = _q(float(base_unit_price) * mult)

    is_spot_buy = False
    if line_rng.random() < SPOT_BUY_PROB:
        is_spot_buy = True
        base_unit_price = _q(float(base_unit_price) * line_rng.uniform(*SPOT_BUY_MULT_RANGE))

    normalized_unit_price = _q(float(base_unit_price) * float(markup))

    pack = layout.pack_config_for(sku_name, item.base_uom)
    unit_price = _q(float(normalized_unit_price) * pack.base_units_per_case)
    base_quantity = line_rng.choice([1, 1, 1, 1, 2, 2, 2, 3, 3, 4])
    quantity = max(1, round(base_quantity * VOLUME_TIER_QTY_MULTIPLIER[tenant.volume_tier]))
    extended_price = _q(float(unit_price) * quantity)

    description = layout.description_for(sku_name)
    if truncate:
        cut = line_rng.randint(max(4, len(description) - 10), max(5, len(description) - 2))
        description = description[:cut]

    raw_sku = None if omit_sku_column else layout.sku_code(sku_name)

    extracted = {
        "line_number": line_number,
        "raw_sku": raw_sku,
        "raw_description": description,
        "raw_pack_size": pack.raw_pack_size,
        "quantity": str(quantity),
        "uom": pack.case_uom,
        "unit_price": str(unit_price),
        "extended_price": str(extended_price),
        "confidence": 1.0,
    }
    meta = {
        "line_number": line_number,
        "canonical_sku_name": sku_name,
        "normalized_unit_price_base": str(normalized_unit_price),
        "base_uom": item.base_uom.value,
        "is_creep_injected": is_creep_injected,
        "is_spot_buy": is_spot_buy,
    }
    return extracted, meta


def _generate_invoice(tenant: SyntheticTenant, week: int, basket: list[str], creep_by_sku: dict[str, dict]) -> dict:
    layout = LAYOUTS[tenant.distributor_slug]
    invoice_rng = rng_for("invoice", tenant.index, week)
    markup = tenant_distributor_markup(rng_for("markup", tenant.index))

    is_noisy = invoice_rng.random() < NOISY_FRACTION
    scan_noise = is_noisy and invoice_rng.random() < 0.6
    omit_sku_column = is_noisy and "sku" in layout.column_order and invoice_rng.random() < 0.3

    sku_names = _week_selection(tenant, week, basket)
    extracted_lines = []
    meta_lines = []
    any_truncated = False
    for i, sku_name in enumerate(sku_names, start=1):
        line_rng = rng_for("line", tenant.index, week, sku_name)
        truncate = is_noisy and line_rng.random() < TRUNCATION_PROB_ON_NOISY_LINE
        any_truncated = any_truncated or truncate
        extracted, meta = _build_line(
            tenant, layout, i, sku_name, week, markup, creep_by_sku, omit_sku_column, truncate, line_rng
        )
        extracted_lines.append(extracted)
        meta_lines.append(meta)

    subtotal = _q(sum(Decimal(li["extended_price"]) for li in extracted_lines))
    tax = _q(0)
    total = _q(subtotal + tax)

    invoice_date = START_DATE + timedelta(weeks=week)
    delivery_date = invoice_date + timedelta(days=1)
    invoice_number = f"{layout.slug.upper()}-{tenant.index:02d}-{week:03d}"

    extracted_invoice = {
        "distributor": layout.slug,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date.isoformat(),
        "delivery_date": delivery_date.isoformat(),
        "subtotal": str(subtotal),
        "tax": str(tax),
        "total": str(total),
        "line_items": extracted_lines,
    }

    noise_types = []
    if scan_noise:
        noise_types.append("scan_skew_jpeg")
    if omit_sku_column:
        noise_types.append("missing_sku_column")
    if any_truncated:
        noise_types.append("truncated_description")

    ground_truth = {
        **extracted_invoice,
        "_meta": {
            "tenant_index": tenant.index,
            "tenant_name": tenant.name,
            "metro": tenant.metro,
            "volume_tier": tenant.volume_tier,
            "distributor_slug": layout.slug,
            "week_index": week,
            "noisy": is_noisy,
            "noise_types": noise_types,
            "line_items_meta": meta_lines,
        },
    }

    columns = layout.column_order
    if omit_sku_column:
        columns = tuple(c for c in columns if c != "sku")

    render_rows = []
    for extracted in extracted_lines:
        render_rows.append(
            {
                "line_number": extracted["line_number"],
                "sku": extracted["raw_sku"] or "",
                "description": extracted["raw_description"],
                "pack_size": extracted["raw_pack_size"],
                "qty": extracted["quantity"],
                "uom": extracted["uom"],
                "unit_price": extracted["unit_price"],
                "ext_price": extracted["extended_price"],
            }
        )

    header = {
        "tenant_name": tenant.name,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date.isoformat(),
        "delivery_date": delivery_date.isoformat(),
    }
    totals = {"subtotal": str(subtotal), "tax": str(tax), "total": str(total)}

    pdf_bytes = render_invoice_pdf(layout, header, columns, render_rows, totals)
    if scan_noise:
        pdf_bytes = apply_scan_noise(pdf_bytes, rng_for("scan_noise", tenant.index, week))

    return {"pdf_bytes": pdf_bytes, "ground_truth": ground_truth, "invoice_number": invoice_number}


def main() -> None:
    # Clear previous tenant_*/ output and manifest.json, but leave the directory
    # (and its .gitkeep, tracked so the empty path survives a fresh clone) alone.
    if OUT_DIR.exists():
        for child in OUT_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tenants = build_tenants()
    manifest = {
        "master_seed": MASTER_SEED,
        "weeks": WEEKS,
        "tenants": [asdict(t) for t in tenants],
        "creep_injections": [],
        "counts": {"invoices": 0, "line_items": 0, "by_distributor": {}},
    }

    for tenant in tenants:
        basket = _tenant_basket(tenant)
        creep_list = _tenant_creep_skus(tenant, basket)
        creep_by_sku = {c["canonical_sku_name"]: c for c in creep_list}
        for c in creep_list:
            manifest["creep_injections"].append({"tenant_index": tenant.index, "tenant_name": tenant.name, **c})

        tenant_dir = OUT_DIR / f"tenant_{tenant.index:02d}"
        tenant_dir.mkdir(parents=True, exist_ok=True)

        for week in range(WEEKS):
            result = _generate_invoice(tenant, week, basket, creep_by_sku)
            stem = f"week_{week:02d}"
            (tenant_dir / f"{stem}.pdf").write_bytes(result["pdf_bytes"])
            (tenant_dir / f"{stem}.json").write_text(json.dumps(result["ground_truth"], indent=2))

            manifest["counts"]["invoices"] += 1
            manifest["counts"]["line_items"] += len(result["ground_truth"]["line_items"])
            dist = tenant.distributor_slug
            manifest["counts"]["by_distributor"][dist] = manifest["counts"]["by_distributor"].get(dist, 0) + 1

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(
        f"Generated {manifest['counts']['invoices']} invoices, "
        f"{manifest['counts']['line_items']} line items, "
        f"{len(manifest['creep_injections'])} creep injections."
    )


if __name__ == "__main__":
    main()
