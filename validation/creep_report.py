"""Phase 4a gate: python -m validation.creep_report

Runs app.analytics.price_creep.detect_price_creep for every synthetic tenant
against the price_observations seeded by `make seed-analytics`, and scores
the flagged (tenant, canonical_sku) pairs against synthetic/out/manifest.json's
injected-creep ground truth: recall (did we catch the real creep events) and
false positives (did we flag anything on a stable SKU). Prints a
human-readable table, writes JSON to validation/results/, and exits non-zero
if either Phase 4a threshold is missed — false_positive_max is a hard zero,
not tunable (SPEC.md §7: "false positives here are expensive").
"""
import json
import sys
from pathlib import Path

import yaml
from sqlalchemy import select

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.analytics.price_creep import detect_price_creep  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.canonical_sku import CanonicalSku  # noqa: E402
from app.models.tenant import Tenant  # noqa: E402

OUT_DIR = REPO_ROOT / "synthetic" / "out"
THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "phase4_creep.json"


def main() -> int:
    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase4_analytics"]["creep"]

    manifest_path = OUT_DIR / "manifest.json"
    if not manifest_path.exists():
        print(f"FAIL: {manifest_path} missing. Run `make seed` first.")
        return 1
    manifest = json.loads(manifest_path.read_text())
    creep_injections = manifest["creep_injections"]
    true_creep_keys = {(c["tenant_name"], c["canonical_sku_name"]) for c in creep_injections}

    db = SessionLocal()
    name_by_id = {row.id: row.name for row in db.scalars(select(CanonicalSku))}

    tenants_by_name = {t.name: t for t in db.scalars(select(Tenant)) if t.name in {c["tenant_name"] for c in creep_injections}}
    missing = {c["tenant_name"] for c in creep_injections} - set(tenants_by_name)
    if missing:
        print(f"FAIL: tenants not found in DB: {sorted(missing)}. Run `make seed-analytics` first.")
        return 1

    true_positives = 0
    false_negatives = 0
    false_positives: list[tuple[str, str]] = []
    flagged_by_tenant: dict[str, set[str]] = {}

    for tenant_name, tenant in tenants_by_name.items():
        findings = detect_price_creep(db, tenant.id)
        flagged_names = {name_by_id[f.canonical_sku_id] for f in findings if f.canonical_sku_id in name_by_id}
        flagged_by_tenant[tenant_name] = flagged_names
        for sku_name in flagged_names:
            if (tenant_name, sku_name) not in true_creep_keys:
                false_positives.append((tenant_name, sku_name))

    for tenant_name, sku_name in true_creep_keys:
        if sku_name in flagged_by_tenant.get(tenant_name, set()):
            true_positives += 1
        else:
            false_negatives += 1

    total_creep = len(true_creep_keys)
    recall = true_positives / total_creep if total_creep else 0.0
    false_positive_count = len(false_positives)

    print("=== Phase 4a price-creep report ===")
    print(f"Injected creep events: {total_creep}")
    print(f"True positives: {true_positives}  False negatives: {false_negatives}  Recall: {recall:.1%}")
    print(f"False positives on stable SKUs: {false_positive_count}")
    if false_positives:
        print("Sample false positives:")
        for tenant_name, sku_name in false_positives[:10]:
            print(f"  {tenant_name:20s} {sku_name}")

    failures = []
    if recall < thresholds["recall_min"]:
        failures.append(f"recall {recall:.3f} < {thresholds['recall_min']}")
    if false_positive_count > thresholds["false_positive_max"]:
        failures.append(f"false_positive_count {false_positive_count} > {thresholds['false_positive_max']}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "total_creep_injections": total_creep,
                "true_positives": true_positives,
                "false_negatives": false_negatives,
                "recall": recall,
                "false_positive_count": false_positive_count,
                "false_positives": [{"tenant": t, "sku": s} for t, s in false_positives],
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
