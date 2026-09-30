"""Phase 2 gate: python -m validation.extraction_report --sample N

Runs real extraction (or, with --fake, the deterministic fake extractor —
a zero-cost dry run of the reporting pipeline itself) against a
random sample of the synthetic corpus and scores it against ground truth:
per-field accuracy, line-level match rate (both split clean vs. noisy per
SPEC.md's separate thresholds), the arithmetic-validator's catch rate on
deliberately digit-corrupted lines, review-queue routing rate, and mean cost
per invoice. Writes JSON to validation/results/ and exits non-zero if any
Phase 2 threshold in validation/thresholds.yaml is missed.

Every invoice in the sample costs real money once a real API key is configured
— this script does not ask for confirmation itself; whoever runs it (a human,
or Claude Code following an explicit instruction) is responsible for choosing
a sample size appropriate to what's been approved to spend.
"""
import argparse
import json
import random
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

import anthropic
import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.extract.client import AnthropicExtractorClient, ExtractionFailedError, FakeExtractorClient, cost_usd  # noqa: E402
from app.extract.confidence import ARITHMETIC_TOLERANCE, assess_extraction  # noqa: E402
from app.extract.schema import ExtractedInvoice  # noqa: E402
from app.ingest.render import render_pdf_to_pngs  # noqa: E402

OUT_DIR = REPO_ROOT / "synthetic" / "out"
THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "phase2_extraction.json"

COMPARE_FIELDS = ["raw_description", "raw_sku", "raw_pack_size", "uom"]
MONEY_FIELDS = ["quantity", "unit_price", "extended_price"]


def _load_ground_truth(path: Path) -> tuple[ExtractedInvoice, dict]:
    data = json.loads(path.read_text())
    meta = data.pop("_meta")
    return ExtractedInvoice.model_validate(data), meta


def _money_equal(a: str, b: str) -> bool:
    try:
        return abs(Decimal(a) - Decimal(b)) < Decimal("0.01")
    except InvalidOperation:
        return a == b


def _score_invoice(extracted: ExtractedInvoice, truth: ExtractedInvoice) -> dict:
    truth_by_line = {li.line_number: li for li in truth.line_items}
    extracted_by_line = {li.line_number: li for li in extracted.line_items}

    matched_line_numbers = set(truth_by_line) & set(extracted_by_line)
    line_recall = len(matched_line_numbers) / len(truth_by_line) if truth_by_line else 1.0
    line_precision = len(matched_line_numbers) / len(extracted_by_line) if extracted_by_line else 1.0

    field_correct = dict.fromkeys(COMPARE_FIELDS + MONEY_FIELDS, 0)
    field_total = dict.fromkeys(COMPARE_FIELDS + MONEY_FIELDS, 0)
    for ln in matched_line_numbers:
        t, e = truth_by_line[ln], extracted_by_line[ln]
        for f in COMPARE_FIELDS:
            # A key with no unit (none printed on the invoice) has nothing
            # to score the unit against.
            if f == "uom" and not t.uom:
                continue
            field_total[f] += 1
            if getattr(t, f) == getattr(e, f):
                field_correct[f] += 1
        for f in MONEY_FIELDS:
            field_total[f] += 1
            if _money_equal(getattr(t, f), getattr(e, f)):
                field_correct[f] += 1

    return {
        "line_recall": line_recall,
        "line_precision": line_precision,
        "field_correct": field_correct,
        "field_total": field_total,
    }


def _corrupt_a_digit(value: str, rng: random.Random) -> str:
    digits = [c for c in value if c.isdigit()]
    if not digits:
        return value
    idx = rng.randrange(len(value))
    while not value[idx].isdigit():
        idx = rng.randrange(len(value))
    new_digit = rng.choice([d for d in "0123456789" if d != value[idx]])
    return value[:idx] + new_digit + value[idx + 1 :]


def _line_error(line, field: str, value: str) -> Decimal:
    """How far a line's quantity x unit price is from its extended price, with
    `field` replaced by `value`."""
    numbers = {name: Decimal(value if name == field else getattr(line, name)) for name in MONEY_FIELDS}
    return abs(numbers["quantity"] * numbers["unit_price"] - numbers["extended_price"])


def _arithmetic_catch_rate(extracted: ExtractedInvoice, rng: random.Random) -> tuple[int, int, int]:
    """Corrupts one digit in a random money field on each line, re-checks
    assess_extraction, and returns (caught, total, below_tolerance) — SPEC.md's
    "deliberately corrupted invoices" catch-rate metric for the arithmetic
    validator itself.

    A corruption only counts toward the rate if it moves the line's arithmetic
    by more than ARITHMETIC_TOLERANCE, the one cent the validator deliberately
    allows for the rounding invoices apply to quantity x unit price. The corpus
    prints money to four decimals ("131.4920"), so roughly a fifth of random
    digit swaps land in the third or fourth decimal and move the line by less
    than a cent. Counting those made the rate report ~0.80 for a validator
    that, measured on the whole corpus, catches every corruption larger than
    its tolerance. Real invoices print two decimals, where sub-cent changes
    can't happen. Excluded corruptions are counted and reported rather than
    dropped silently (validation/thresholds.yaml).
    """
    if not extracted.line_items:
        return 0, 0, 0
    caught = 0
    total = 0
    below_tolerance = 0
    for line in extracted.line_items:
        field = rng.choice(MONEY_FIELDS)
        original = getattr(line, field)
        corrupted = _corrupt_a_digit(original, rng)
        if corrupted == original:
            continue
        try:
            moved = abs(_line_error(line, field, corrupted) - _line_error(line, field, original))
        except InvalidOperation:
            moved = None  # unparseable either way: a corruption the validator must catch
        if moved is not None and moved <= ARITHMETIC_TOLERANCE:
            below_tolerance += 1
            continue
        total += 1
        corrupted_invoice = extracted.model_copy(deep=True)
        for li in corrupted_invoice.line_items:
            if li.line_number == line.line_number:
                setattr(li, field, corrupted)
        assessment = assess_extraction(corrupted_invoice)
        if line.line_number in assessment.failed_line_numbers or assessment.status.value == "needs_review":
            caught += 1
    return caught, total, below_tolerance


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--start",
        type=int,
        default=0,
        help="Skip the first N invoices of the (seeded) sample: resume a run the "
        "API cut short without paying again for invoices already measured.",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Use the deterministic fake extractor instead of the real API — "
        "for a zero-cost dry run of the reporting pipeline's mechanics only. "
        "Accuracy/cost numbers from a --fake run are meaningless and always PASS.",
    )
    args = parser.parse_args()

    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase2_extraction"]

    gt_files = sorted(OUT_DIR.glob("tenant_*/week_*.json"))
    if not gt_files:
        print("FAIL: synthetic/out/ is empty. Run `make seed` first.")
        return 1

    rng = random.Random(args.seed)
    sample = rng.sample(gt_files, min(args.sample, len(gt_files)))[args.start :]

    extractor = FakeExtractorClient() if args.fake else AnthropicExtractorClient()
    print(f"Extractor: {type(extractor).__name__}, model={getattr(extractor, 'model', 'n/a')}")
    print(f"Sample size: {len(sample)}")

    per_invoice = []
    stopped_early = None
    with tempfile.TemporaryDirectory() as tmp:
        for i, gt_path in enumerate(sample):
            if stopped_early:
                break
            truth, meta = _load_ground_truth(gt_path)
            pdf_path = gt_path.with_suffix(".pdf")
            page_paths = render_pdf_to_pngs(pdf_path.read_bytes(), Path(tmp) / f"inv_{i}")

            # One failed invoice must not abort the run. The first real run
            # died at invoice 13 of 20 and threw away the twelve already paid
            # for. A failure is scored as extracting nothing: every true line
            # missed, which is what it would mean for a customer's invoice.
            failed_reason = None
            try:
                extracted, cost = extractor.extract(page_paths)
            except anthropic.APIError as exc:
                # The API itself refusing (credit exhausted, rate limit,
                # network) is not an extraction failure and says nothing about
                # accuracy. Stop, and still report on everything already paid
                # for: a crash here used to lose the whole summary.
                stopped_early = f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"
                print(f"  [{i+1}/{len(sample)}] STOPPED: {stopped_early}", flush=True)
                break
            except ExtractionFailedError as exc:
                failed_reason, cost = str(exc), exc.cost_usd
                extracted = ExtractedInvoice(
                    distributor="other", invoice_number="", invoice_date="", subtotal="0", tax="0", total="0",
                    line_items=[],
                )
            score = _score_invoice(extracted, truth)
            assessment = assess_extraction(extracted)
            caught, corrupted_total, below_tolerance = _arithmetic_catch_rate(extracted, rng)

            per_invoice.append(
                {
                    "path": str(gt_path),
                    "noisy": meta["noisy"],
                    "cost_usd": cost,
                    "needs_review": assessment.status.value == "needs_review",
                    "arithmetic_caught": caught,
                    "arithmetic_total": corrupted_total,
                    "arithmetic_below_tolerance": below_tolerance,
                    "true_lines": len(truth.line_items),
                    "extraction_failed": failed_reason,
                    **score,
                }
            )
            status = f"FAILED: {failed_reason}" if failed_reason else f"line recall {score['line_recall']:.3f}"
            print(
                f"  [{i+1}/{len(sample)}] {gt_path.parent.name}/{gt_path.stem} noisy={meta['noisy']} "
                f"lines={len(truth.line_items)} cost=${cost:.4f} {status}",
                flush=True,
            )

    def _field_accuracy(rows: list[dict], field: str) -> float | None:
        correct = sum(r["field_correct"][field] for r in rows)
        total = sum(r["field_total"][field] for r in rows)
        return correct / total if total else None

    def _summarize(rows: list[dict]) -> dict:
        if not rows:
            return {}
        return {
            "n": len(rows),
            "line_recall": sum(r["line_recall"] for r in rows) / len(rows),
            "line_precision": sum(r["line_precision"] for r in rows) / len(rows),
            "field_accuracy": {f: _field_accuracy(rows, f) for f in COMPARE_FIELDS + MONEY_FIELDS},
        }

    clean_rows = [r for r in per_invoice if not r["noisy"]]
    noisy_rows = [r for r in per_invoice if r["noisy"]]

    total_cost = sum(r["cost_usd"] for r in per_invoice)
    mean_cost = total_cost / len(per_invoice) if per_invoice else 0.0
    review_rate = sum(r["needs_review"] for r in per_invoice) / len(per_invoice) if per_invoice else 0.0
    arithmetic_caught = sum(r["arithmetic_caught"] for r in per_invoice)
    arithmetic_total = sum(r["arithmetic_total"] for r in per_invoice)
    arithmetic_below_tolerance = sum(r.get("arithmetic_below_tolerance", 0) for r in per_invoice)
    arithmetic_catch_rate = arithmetic_caught / arithmetic_total if arithmetic_total else None

    clean_summary = _summarize(clean_rows)
    noisy_summary = _summarize(noisy_rows)

    if not per_invoice:
        print(f"\nNo invoices completed ({stopped_early}).")
        return 1
    print("\n=== Phase 2 extraction report ===")
    if stopped_early:
        print(f"PARTIAL: stopped after {len(per_invoice)} of {len(sample)} invoices ({stopped_early})")
    print(f"Clean ({len(clean_rows)}): {clean_summary}")
    print(f"Noisy ({len(noisy_rows)}): {noisy_summary}")
    print(f"Mean cost/invoice: ${mean_cost:.4f}  (threshold < ${thresholds['max_cost_per_invoice_usd']})")
    print(f"Review-queue routing rate: {review_rate:.1%}")
    extraction_failures = [r for r in per_invoice if r["extraction_failed"]]
    print(f"Extraction failures (scored as zero lines extracted): {len(extraction_failures)}")
    for r in extraction_failures:
        print(f"  - {Path(r['path']).parent.name}/{Path(r['path']).stem} ({r['true_lines']} lines): {r['extraction_failed']}")
    print(f"Most expensive invoice: ${max(r['cost_usd'] for r in per_invoice):.4f}" if per_invoice else "")
    print(f"Arithmetic-validator catch rate on corrupted lines: {arithmetic_catch_rate} ({arithmetic_caught}/{arithmetic_total})")
    print(
        f"  (excluded {arithmetic_below_tolerance} corruptions that moved a line by <= ${ARITHMETIC_TOLERANCE}, "
        "within the validator's rounding tolerance; see _arithmetic_catch_rate)"
    )

    failures = []
    clean_line_acc = clean_summary.get("line_recall")
    if clean_line_acc is not None and clean_line_acc < thresholds["line_item_accuracy_clean"]:
        failures.append(f"clean line-item accuracy {clean_line_acc:.3f} < {thresholds['line_item_accuracy_clean']}")
    noisy_line_acc = noisy_summary.get("line_recall")
    if noisy_line_acc is not None and noisy_line_acc < thresholds["line_item_accuracy_noisy"]:
        failures.append(f"noisy line-item accuracy {noisy_line_acc:.3f} < {thresholds['line_item_accuracy_noisy']}")
    if arithmetic_catch_rate is not None and arithmetic_catch_rate < thresholds["arithmetic_error_catch_rate"]:
        failures.append(f"arithmetic catch rate {arithmetic_catch_rate:.3f} < {thresholds['arithmetic_error_catch_rate']}")
    if mean_cost > thresholds["max_cost_per_invoice_usd"]:
        failures.append(f"mean cost ${mean_cost:.4f} > ${thresholds['max_cost_per_invoice_usd']}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "sample_size": len(per_invoice),
                "requested_sample_size": len(sample),
                "stopped_early": stopped_early,
                "clean": clean_summary,
                "noisy": noisy_summary,
                "mean_cost_usd": mean_cost,
                "review_rate": review_rate,
                "arithmetic_catch_rate": arithmetic_catch_rate,
                "extraction_failures": len(extraction_failures),
                "per_invoice": [
                    {k: r[k] for k in ("path", "noisy", "true_lines", "cost_usd", "line_recall", "extraction_failed")}
                    for r in per_invoice
                ],
                "failures": failures,
            },
            indent=2,
        )
    )

    if stopped_early:
        # Numbers over a partial sample are reported, but a run that didn't
        # finish can't pass the gate it was asked to measure.
        failures.append(f"incomplete run: {len(per_invoice)} of {len(sample)} invoices ({stopped_early})")
    if args.fake:
        # The fake extractor returns one fixed invoice for every page, so its
        # accuracy is meaningless by design; a dry run checks that rendering,
        # scoring and reporting run end to end, and nothing more.
        print(f"\nDRY RUN (--fake): pipeline ran on {len(per_invoice)} invoice(s); thresholds not applied")
        return 0
    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
