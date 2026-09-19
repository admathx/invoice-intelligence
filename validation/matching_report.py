"""Phase 3c gate: python -m validation.matching_report

Runs the embedding matcher over every line item in the synthetic corpus (not a
sample — SPEC.md's exit criteria says "of corpus line items") and scores it
against ground truth (the synthetic generator's own `canonical_sku_name`,
which is known-true by construction). Prints auto-match rate, review rate, and
— the number that matters — false match rate. Writes JSON to
validation/results/ and exits non-zero if any Phase 3 threshold is missed.

Deduplicates by (raw_description, raw_pack_size): the synthetic corpus's
(distributor, canonical item) descriptions and pack sizes are stable across
all 26 weeks (see synthetic/generate.py's precomputed _DESCRIPTIONS/
_PACK_CONFIGS caches), so ~39,000 line items collapse to a few hundred
distinct match computations — batch-embedding those instead of one API-free
but still real model call per line is the difference between seconds and
tens of minutes.
"""
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import numpy as np
import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.db import SessionLocal  # noqa: E402
from app.models.canonical_sku import CanonicalSku  # noqa: E402
from app.normalize.description_expansion import normalize_for_embedding  # noqa: E402
from app.normalize.embeddings import embed_texts  # noqa: E402
from app.normalize.pack_size import PackSizeParseError, parse_pack_size  # noqa: E402
from sqlalchemy import select  # noqa: E402

OUT_DIR = REPO_ROOT / "synthetic" / "out"
THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "phase3_normalization.json"

AUTO_MATCH_CONFIDENCE_THRESHOLD = Decimal("0.92")
REVIEW_QUEUE_CONFIDENCE_LOW = Decimal("0.80")


def _load_candidates(db):
    rows = list(db.scalars(select(CanonicalSku).where(CanonicalSku.description_embedding.is_not(None))))
    ids = [r.id for r in rows]
    names = [r.name for r in rows]
    base_uoms = [r.base_uom for r in rows]
    matrix = np.array([r.description_embedding for r in rows], dtype=np.float32)
    return ids, names, base_uoms, matrix


def _best_match(query_vec: np.ndarray, compatible_uoms: set, ids, names, base_uoms, matrix):
    mask = np.array([u in compatible_uoms for u in base_uoms])
    if not mask.any():
        return None, None, None
    sims = matrix[mask] @ query_vec
    best_idx_local = int(np.argmax(sims))
    global_indices = np.nonzero(mask)[0]
    best_idx = global_indices[best_idx_local]
    return ids[best_idx], names[best_idx], float(sims[best_idx_local])


def main() -> int:
    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase3_normalization"]

    gt_files = sorted(OUT_DIR.glob("tenant_*/week_*.json"))
    if not gt_files:
        print("FAIL: synthetic/out/ is empty. Run `make seed` first.")
        return 1

    db = SessionLocal()
    ids, names, base_uoms, matrix = _load_candidates(db)
    print(f"Loaded {len(ids)} canonical SKUs with embeddings.")

    # Pass 1: collect every (raw_description, raw_pack_size) pair and every line's truth label.
    all_lines: list[dict] = []
    unique_keys: dict[tuple[str, str], None] = {}
    pack_size_failures = 0

    for gt_path in gt_files:
        data = json.loads(gt_path.read_text())
        meta = data["_meta"]
        for li, m in zip(data["line_items"], meta["line_items_meta"]):
            key = (li["raw_description"], li["raw_pack_size"])
            unique_keys[key] = None
            all_lines.append(
                {
                    "key": key,
                    "true_name": m["canonical_sku_name"],
                }
            )

    print(f"{len(all_lines)} total line items, {len(unique_keys)} distinct (description, pack_size) pairs.")

    # Pass 2: batch-embed the distinct normalized descriptions.
    distinct_keys = list(unique_keys.keys())
    normalized_texts = [normalize_for_embedding(desc) for desc, _ in distinct_keys]
    vectors = embed_texts(normalized_texts)
    print("Batch embedding complete.")

    # Pass 3: match each distinct key once, cache the result.
    match_cache: dict[tuple[str, str], dict] = {}
    for (desc, pack_str), vec in zip(distinct_keys, vectors):
        try:
            pack = parse_pack_size(pack_str)
        except PackSizeParseError:
            match_cache[(desc, pack_str)] = {"status": "unparseable_pack_size"}
            continue

        candidate_id, candidate_name, similarity = _best_match(
            np.array(vec, dtype=np.float32), pack.compatible_base_uoms, ids, names, base_uoms, matrix
        )
        if candidate_id is None:
            match_cache[(desc, pack_str)] = {"status": "no_candidates"}
            continue

        sim_decimal = Decimal(str(round(similarity, 4)))
        if sim_decimal >= AUTO_MATCH_CONFIDENCE_THRESHOLD:
            status = "auto"
        elif sim_decimal >= REVIEW_QUEUE_CONFIDENCE_LOW:
            status = "review"
        else:
            status = "new_candidate"
        match_cache[(desc, pack_str)] = {
            "status": status,
            "matched_name": candidate_name,
            "similarity": similarity,
        }

    # Pass 4: score every line item against ground truth using the cache.
    counts = Counter()
    false_matches = []
    for line in all_lines:
        result = match_cache[line["key"]]
        status = result["status"]
        counts[status] += 1
        if status in ("auto", "review"):
            if result["matched_name"] != line["true_name"]:
                counts[f"{status}_wrong"] += 1
                if status == "auto":
                    false_matches.append((line["key"][0], result["matched_name"], line["true_name"]))

    total = len(all_lines)
    auto_rate = counts["auto"] / total
    review_rate = (counts["review"] + counts["new_candidate"]) / total
    auto_wrong = counts["auto_wrong"]
    false_match_rate = auto_wrong / counts["auto"] if counts["auto"] else 0.0

    print("\n=== Phase 3 matching report ===")
    print(f"Total line items: {total}")
    print(f"Auto-match rate (>={AUTO_MATCH_CONFIDENCE_THRESHOLD}): {auto_rate:.1%} ({counts['auto']}/{total})")
    print(f"Review-queue rate (0.80-0.92 or new candidate): {review_rate:.1%}")
    print(f"Unparseable pack size: {counts['unparseable_pack_size']}")
    print(f"False match rate among auto-matches: {false_match_rate:.4%} ({auto_wrong}/{counts['auto']})")
    if false_matches:
        print("Sample false matches:")
        for desc, matched, true in false_matches[:10]:
            print(f"  {desc!r} -> matched {matched!r}, true {true!r}")

    # Spot-check: the same physical product from two different distributors
    # resolves to one canonical_sku_id, for 20 SKUs that appear under >=2
    # distributors in the corpus.
    by_name_distributors: dict[str, set[str]] = {}
    name_to_matched_ids: dict[str, set] = {}
    for gt_path in gt_files:
        data = json.loads(gt_path.read_text())
        dist = data["_meta"]["distributor_slug"]
        for li, m in zip(data["line_items"], data["_meta"]["line_items_meta"]):
            true_name = m["canonical_sku_name"]
            by_name_distributors.setdefault(true_name, set()).add(dist)
            result = match_cache[(li["raw_description"], li["raw_pack_size"])]
            # Only auto/review matches carry an actual resolved candidate — a
            # line that fell to new_candidate/unparseable contributes no
            # candidate id, and must NOT count as "a different id" (that would
            # conflate "unresolved" with "resolved to the wrong SKU").
            if result["status"] in ("auto", "review") and result["matched_name"] in names:
                matched_id = ids[names.index(result["matched_name"])]
                name_to_matched_ids.setdefault(true_name, set()).add(matched_id)

    multi_distributor_names = [n for n, dists in by_name_distributors.items() if len(dists) >= 2][:20]
    consistent = sum(1 for n in multi_distributor_names if len(name_to_matched_ids.get(n, set())) <= 1)
    print(f"\nCross-distributor consistency spot-check: {consistent}/{len(multi_distributor_names)} SKUs "
          f"resolved to at most one canonical_sku_id across distributors (excluding unresolved lines).")

    failures = []
    if auto_rate < thresholds["auto_match_rate"]:
        failures.append(f"auto_match_rate {auto_rate:.3f} < {thresholds['auto_match_rate']}")
    if false_match_rate > thresholds["false_match_rate_max"]:
        failures.append(f"false_match_rate {false_match_rate:.4f} > {thresholds['false_match_rate_max']}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "total_line_items": total,
                "auto_match_rate": auto_rate,
                "review_rate": review_rate,
                "false_match_rate": false_match_rate,
                "unparseable_pack_size": counts["unparseable_pack_size"],
                "cross_distributor_consistency": f"{consistent}/{len(multi_distributor_names)}",
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
