"""Phase 1 gate, per SPEC.md §10: asserts the synthetic generator is internally
consistent. Requires `make seed` to have been run first (these tests read
synthetic/out/, they don't regenerate it — regeneration is its own slow step).
"""
import json
from pathlib import Path

import pytest

OUT_DIR = Path(__file__).parent.parent.parent / "synthetic" / "out"
GROUND_TRUTH_FILES = sorted(OUT_DIR.glob("tenant_*/week_*.json"))

pytestmark = pytest.mark.skipif(
    not GROUND_TRUTH_FILES, reason="synthetic/out/ is empty — run `make seed` first"
)


def _load_all() -> list[dict]:
    return [json.loads(p.read_text()) for p in GROUND_TRUTH_FILES]


def test_every_pdf_has_ground_truth():
    pdf_stems = {p.relative_to(OUT_DIR).with_suffix("") for p in OUT_DIR.glob("tenant_*/week_*.pdf")}
    json_stems = {p.relative_to(OUT_DIR).with_suffix("") for p in GROUND_TRUTH_FILES}
    assert pdf_stems == json_stems
    assert len(pdf_stems) == 20 * 26


def test_line_extendeds_sum_to_subtotal():
    for gt in _load_all():
        line_sum = sum(float(li["extended_price"]) for li in gt["line_items"])
        assert abs(line_sum - float(gt["subtotal"])) < 0.01, gt["invoice_number"]


def test_subtotal_plus_tax_equals_total():
    for gt in _load_all():
        assert abs((float(gt["subtotal"]) + float(gt["tax"])) - float(gt["total"])) < 0.01, gt["invoice_number"]


def test_injected_creep_skus_show_intended_trajectory():
    manifest = json.loads((OUT_DIR / "manifest.json").read_text())
    ground_truths = _load_all()
    by_tenant: dict[int, list[dict]] = {}
    for gt in ground_truths:
        by_tenant.setdefault(gt["_meta"]["tenant_index"], []).append(gt)

    checked = 0
    for creep in manifest["creep_injections"]:
        tenant_idx = creep["tenant_index"]
        sku_name = creep["canonical_sku_name"]
        start = creep["creep_start_week"]

        # Whichever weeks happened to include this SKU: pre-creep window average
        # vs. the final few weeks' average. A week-by-week exact lookup isn't
        # reliable here since each week only orders a random subset of the basket.
        pre_prices = _prices_for(by_tenant[tenant_idx], sku_name, week_max=start - 1)
        post_prices = _prices_for(by_tenant[tenant_idx], sku_name, week_min=22)
        if len(pre_prices) < 2 or len(post_prices) < 2:
            continue  # too sparse for this particular (tenant, sku) to judge; not a failure

        pre_avg = sum(pre_prices) / len(pre_prices)
        post_avg = sum(post_prices) / len(post_prices)
        actual_pct = (post_avg - pre_avg) / pre_avg
        assert actual_pct > 0.03, f"{creep['tenant_name']}/{sku_name}: expected upward creep, got {actual_pct:.3f}"
        checked += 1

    assert checked >= len(manifest["creep_injections"]) * 0.5, "too few creep injections were verifiable"


def _prices_for(tenant_gts: list[dict], sku_name: str, week_min: int = 0, week_max: int = 25) -> list[float]:
    prices = []
    for gt in tenant_gts:
        week = gt["_meta"]["week_index"]
        if not (week_min <= week <= week_max):
            continue
        for meta in gt["_meta"]["line_items_meta"]:
            if meta["canonical_sku_name"] == sku_name:
                prices.append(float(meta["normalized_unit_price_base"]))
                break
    return prices


def test_stable_skus_stay_within_noise_band():
    manifest = json.loads((OUT_DIR / "manifest.json").read_text())
    creep_pairs = {(c["tenant_index"], c["canonical_sku_name"]) for c in manifest["creep_injections"]}
    ground_truths = _load_all()
    by_tenant: dict[int, list[dict]] = {}
    for gt in ground_truths:
        by_tenant.setdefault(gt["_meta"]["tenant_index"], []).append(gt)

    tenant0_gt = by_tenant.get(0, [])
    all_names = {m["canonical_sku_name"] for gt in tenant0_gt for m in gt["_meta"]["line_items_meta"]}
    stable_names = [n for n in sorted(all_names) if (0, n) not in creep_pairs]

    checked = 0
    for sku_name in stable_names:
        pre_prices = _prices_for(tenant0_gt, sku_name, week_max=7)
        post_prices = _prices_for(tenant0_gt, sku_name, week_min=18)
        if len(pre_prices) < 2 or len(post_prices) < 2:
            continue
        pre_avg = sum(pre_prices) / len(pre_prices)
        post_avg = sum(post_prices) / len(post_prices)
        pct_change = abs(post_avg - pre_avg) / pre_avg
        # Comfortably above the seasonal+noise band any single stable SKU should
        # show, but well below the smallest injected creep target (10%).
        assert pct_change < 0.09, f"tenant 0 / {sku_name}: stable SKU moved {pct_change:.3f}"
        checked += 1
        if checked >= 20:
            break

    assert checked > 0, "no stable SKUs were verifiable for tenant 0"
