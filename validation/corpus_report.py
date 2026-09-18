"""Phase 1 gate: validates and summarizes the synthetic corpus in synthetic/out/.

Scans the ground-truth JSON files directly (not just the manifest the generator
wrote) so this is an independent check, not a self-report. Prints a human-readable
table, writes JSON to validation/results/, and exits non-zero if anything the spec's
Phase 1 exit criteria calls for doesn't hold.
"""
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
OUT_DIR = REPO_ROOT / "synthetic" / "out"
THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "phase1_corpus.json"


def load_ground_truth_files() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(OUT_DIR.glob("tenant_*/week_*.json"))]


def main() -> int:
    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase1_corpus"]

    if not OUT_DIR.exists():
        print(f"FAIL: {OUT_DIR} does not exist. Run `make seed` first.")
        return 1

    manifest_path = OUT_DIR / "manifest.json"
    if not manifest_path.exists():
        print(f"FAIL: {manifest_path} missing.")
        return 1
    manifest = json.loads(manifest_path.read_text())

    ground_truths = load_ground_truth_files()
    pdf_paths = set(OUT_DIR.glob("tenant_*/week_*.pdf"))
    json_paths = set(OUT_DIR.glob("tenant_*/week_*.json"))

    failures = []

    # Every PDF has a paired ground-truth file and vice versa.
    pdf_stems = {p.relative_to(OUT_DIR).with_suffix("") for p in pdf_paths}
    json_stems = {p.relative_to(OUT_DIR).with_suffix("") for p in json_paths}
    if pdf_stems != json_stems:
        missing_json = pdf_stems - json_stems
        missing_pdf = json_stems - pdf_stems
        failures.append(f"PDF/ground-truth pairing mismatch: missing_json={missing_json} missing_pdf={missing_pdf}")

    tenant_indices = {gt["_meta"]["tenant_index"] for gt in ground_truths}
    metros = {gt["_meta"]["metro"] for gt in ground_truths}
    volume_tiers = {gt["_meta"]["volume_tier"] for gt in ground_truths}
    distributors = Counter(gt["distributor"] for gt in ground_truths)
    total_line_items = sum(len(gt["line_items"]) for gt in ground_truths)
    weeks_by_tenant = Counter(gt["_meta"]["tenant_index"] for gt in ground_truths)

    if len(tenant_indices) != thresholds["tenant_count"]:
        failures.append(f"tenant_count={len(tenant_indices)}, expected {thresholds['tenant_count']}")
    if len(metros) != thresholds["metro_count"]:
        failures.append(f"metro_count={len(metros)}, expected {thresholds['metro_count']}")
    if len(volume_tiers) != thresholds["volume_tier_count"]:
        failures.append(f"volume_tier_count={len(volume_tiers)}, expected {thresholds['volume_tier_count']}")
    if len(distributors) != thresholds["distributor_count"]:
        failures.append(f"distributor_count={len(distributors)}, expected {thresholds['distributor_count']}")
    bad_week_counts = {t: n for t, n in weeks_by_tenant.items() if n != thresholds["weeks_per_tenant"]}
    if bad_week_counts:
        failures.append(f"tenants with wrong week count: {bad_week_counts}")

    # Line extendeds sum to the stated subtotal, within a cent, on every invoice.
    for gt in ground_truths:
        line_sum = sum(float(li["extended_price"]) for li in gt["line_items"])
        if abs(line_sum - float(gt["subtotal"])) > 0.01:
            failures.append(f"{gt['invoice_number']}: line sum {line_sum} != subtotal {gt['subtotal']}")
            break  # one example is enough to fail the gate; avoid a wall of output

    creep_injections = manifest["creep_injections"]

    print("=== Phase 1 corpus report ===")
    print(f"Tenants: {len(tenant_indices)}  Metros: {len(metros)}  Volume tiers: {len(volume_tiers)}")
    print(f"Invoices: {len(ground_truths)}  Line items: {total_line_items}")
    print("Invoices by distributor layout:")
    for slug, count in sorted(distributors.items()):
        print(f"  {slug:10s} {count}")
    print(f"Creep injections: {len(creep_injections)}")
    for c in creep_injections[:10]:
        print(f"  {c['tenant_name']:20s} {c['canonical_sku_name']:35s} +{c['target_pct']*100:.1f}%")
    if len(creep_injections) > 10:
        print(f"  ... and {len(creep_injections) - 10} more")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "tenant_count": len(tenant_indices),
                "metro_count": len(metros),
                "volume_tier_count": len(volume_tiers),
                "invoice_count": len(ground_truths),
                "line_item_count": total_line_items,
                "by_distributor": dict(distributors),
                "creep_injection_count": len(creep_injections),
                "failures": failures,
            },
            indent=2,
        )
    )

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
