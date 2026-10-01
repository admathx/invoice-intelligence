"""Extraction accuracy on real invoices: python -m validation.real_invoice_report DIR

The Phase 2 gate was measured on synthetic PDFs, which are clean renders.
This measures the same things on real invoices (scans, phone photos saved as
PDF, faxes), in two steps, so that each invoice is paid for once:

1. Extract (costs API money; --limit caps how many per run):

       python -m validation.real_invoice_report DIR --extract --limit 20

   For each DIR/*.pdf not yet extracted: render, extract, and save the result
   beside it as NAME.extracted.json (with its cost). Also writes
   NAME.truth.json, a copy of the extraction for a person to correct against
   the paper: fix every value that's wrong, add or delete lines, then set
   "_reviewed": true. Starting from the extraction makes labelling a few
   minutes per invoice instead of typing it all.

2. Score (free, any number of times):

       python -m validation.real_invoice_report DIR

   Compares each reviewed truth file with its extraction: line recall and
   precision, per-field accuracy, cost, and what synthetic data can't show,
   how the arithmetic check does on real mistakes: of the invoices extraction
   got wrong in a money field, how many it held for review (caught), and of
   the ones it got right, how many it held anyway (false alarms).

--fake uses the deterministic fake extractor and its own cache files
(NAME.fake-extracted.json, NAME.fake-truth.json): a zero-cost check that the
harness runs, whose numbers mean nothing.
"""
import argparse
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.extract.client import AnthropicExtractorClient, ExtractionFailedError, FakeExtractorClient  # noqa: E402
from app.extract.confidence import assess_extraction  # noqa: E402
from app.extract.schema import ExtractedInvoice  # noqa: E402
from app.ingest.render import render_pdf_to_pngs  # noqa: E402
from validation.extraction_report import COMPARE_FIELDS, MONEY_FIELDS, _money_equal, _score_invoice  # noqa: E402

THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "real_invoices.json"


def _paths(pdf: Path, fake: bool) -> tuple[Path, Path]:
    prefix = "fake-" if fake else ""
    return pdf.with_name(f"{pdf.stem}.{prefix}extracted.json"), pdf.with_name(f"{pdf.stem}.{prefix}truth.json")


# Long enough for a per-minute rate limit to ease.
RETRY_AFTER_SECONDS = 20


def _extract_one(pdf: Path, extractor, fake: bool) -> bool:
    """Read one document and save the result. False when the API itself
    failed (a rate limit, the network) even after one more try: nothing is
    saved, so the next run reads it again, and the others carry on."""
    extracted_path, truth_path = _paths(pdf, fake)
    with tempfile.TemporaryDirectory() as scratch:
        # Rendering takes the app's pdfium lock (app/ingest/pdfium_lock.py):
        # pdfium isn't thread-safe. The API call is what runs in parallel.
        pages = render_pdf_to_pngs(pdf.read_bytes(), Path(scratch))
        for attempt in (1, 2):
            try:
                extracted, cost = extractor.extract(pages)
                failed = None
                break
            except ExtractionFailedError as exc:
                extracted, cost, failed = None, exc.cost_usd, str(exc)
                break
            except Exception as exc:  # not about this document: the API or the network
                if attempt == 2:
                    print(f"  {pdf.name}: NOT READ ({type(exc).__name__}: {exc}); run again to retry", flush=True)
                    return False
                time.sleep(RETRY_AFTER_SECONDS)
    record = {
        "cost_usd": cost,
        "failed": failed,
        "extraction": extracted.model_dump() if extracted else None,
    }
    extracted_path.write_text(json.dumps(record, indent=2))
    if not truth_path.exists():
        template = extracted.model_dump() if extracted else ExtractedInvoice(
            distributor="other", invoice_number="", invoice_date="", subtotal="0", tax="0", total="0", line_items=[]
        ).model_dump()
        truth_path.write_text(json.dumps({"_reviewed": False, **template}, indent=2))
    status = f"FAILED: {failed}" if failed else f"{len(extracted.line_items)} lines"
    print(f"  {pdf.name}: ${cost:.4f} {status}", flush=True)
    return True


def extract_folder(folder: Path, extractor, *, fake: bool, limit: int | None, workers: int = 1) -> list[Path]:
    """Extract every PDF that has no cached extraction yet, up to `limit`,
    `workers` at a time (each is one API request, saved as it finishes, so
    an interrupted run keeps what it read). Returns the PDFs extracted."""
    pending = [pdf for pdf in sorted(folder.glob("*.pdf")) if not _paths(pdf, fake)[0].exists()]
    if limit is not None:
        pending = pending[:limit]
    if workers <= 1:
        done = [_extract_one(pdf, extractor, fake) for pdf in pending]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(lambda pdf: _extract_one(pdf, extractor, fake), pending))
    if not all(done):
        print(f"{done.count(False)} document(s) weren't read (API errors). Run again to read them.")
    return [pdf for pdf, ok in zip(pending, done) if ok]


def _money_mistake(extracted: ExtractedInvoice, truth: ExtractedInvoice) -> bool:
    """Did extraction get any number on the invoice wrong, or miss or invent a line?"""
    if not all(_money_equal(getattr(extracted, f), getattr(truth, f)) for f in ("subtotal", "tax", "total")):
        return True
    e = {li.line_number: li for li in extracted.line_items}
    t = {li.line_number: li for li in truth.line_items}
    if set(e) != set(t):
        return True
    return any(not _money_equal(getattr(e[n], f), getattr(t[n], f)) for n in t for f in MONEY_FIELDS)


def score_folder(folder: Path, *, fake: bool) -> dict:
    rows, awaiting = [], []
    for pdf in sorted(folder.glob("*.pdf")):
        extracted_path, truth_path = _paths(pdf, fake)
        if not (extracted_path.exists() and truth_path.exists()):
            continue
        truth_data = json.loads(truth_path.read_text())
        if not truth_data.pop("_reviewed", False):
            awaiting.append(pdf.name)
            continue
        record = json.loads(extracted_path.read_text())
        # A hand-made key may leave the unit empty where none is printed;
        # scoring skips it (extraction_report._score_invoice).
        for line in truth_data.get("line_items", []):
            if line.get("uom") is None:
                line["uom"] = ""
        for field in ("invoice_number", "invoice_date"):
            if truth_data.get(field) is None:
                truth_data[field] = ""
        truth = ExtractedInvoice.model_validate(truth_data)
        extracted = (
            ExtractedInvoice.model_validate(record["extraction"])
            if record["extraction"]
            else ExtractedInvoice(distributor="other", invoice_number="", invoice_date="", subtotal="0", tax="0", total="0", line_items=[])
        )
        rows.append(
            {
                "invoice": pdf.name,
                "cost_usd": record["cost_usd"],
                "failed": record["failed"],
                "held_for_review": assess_extraction(extracted).status.value == "needs_review",
                "money_mistake": _money_mistake(extracted, truth),
                **_score_invoice(extracted, truth),
            }
        )

    def rate(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    fields = COMPARE_FIELDS + MONEY_FIELDS
    mistakes = [r for r in rows if r["money_mistake"]]
    correct = [r for r in rows if not r["money_mistake"]]
    return {
        "scored": len(rows),
        "awaiting_review": awaiting,
        "line_recall": rate(sum(r["line_recall"] for r in rows), len(rows)),
        "line_precision": rate(sum(r["line_precision"] for r in rows), len(rows)),
        "field_accuracy": {
            f: rate(sum(r["field_correct"][f] for r in rows), sum(r["field_total"][f] for r in rows)) for f in fields
        },
        "mean_cost_usd": rate(sum(r["cost_usd"] for r in rows), len(rows)),
        "max_cost_usd": max((r["cost_usd"] for r in rows), default=None),
        "failed": [r["invoice"] for r in rows if r["failed"]],
        "invoices_with_money_mistakes": len(mistakes),
        "mistakes_caught": sum(r["held_for_review"] for r in mistakes),
        "false_alarms": sum(r["held_for_review"] for r in correct),
        "invoices": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--extract", action="store_true", help="extract PDFs not yet extracted (costs API money)")
    parser.add_argument("--limit", type=int, default=None, help="extract at most this many this run")
    parser.add_argument("--fake", action="store_true", help="fake extractor, separate cache: a free dry run")
    parser.add_argument("--workers", type=int, default=1, help="read this many at once (each is one API request)")
    args = parser.parse_args()
    if not args.folder.is_dir():
        parser.error(f"{args.folder} is not a directory")

    if args.extract:
        extractor = FakeExtractorClient() if args.fake else AnthropicExtractorClient()
        pending = [p for p in sorted(args.folder.glob("*.pdf")) if not _paths(p, args.fake)[0].exists()]
        count = len(pending) if args.limit is None else min(len(pending), args.limit)
        print(f"Extracting {count} of {len(pending)} not-yet-extracted invoice(s) with {type(extractor).__name__}")
        extract_folder(args.folder, extractor, fake=args.fake, limit=args.limit, workers=args.workers)

    summary = score_folder(args.folder, fake=args.fake)
    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase2_extraction"]
    print(f"\nScored {summary['scored']} reviewed invoice(s); {len(summary['awaiting_review'])} awaiting review")
    if summary["scored"]:
        print(f"  line recall {summary['line_recall']:.3f}   precision {summary['line_precision']:.3f}")
        print(
            f"  (synthetic gates: clean >= {thresholds['line_item_accuracy_clean']}, "
            f"noisy >= {thresholds['line_item_accuracy_noisy']})"
        )
        for name, accuracy in summary["field_accuracy"].items():
            if accuracy is not None:
                print(f"  {name:<16} {accuracy:.3f}")
        print(f"  cost: mean ${summary['mean_cost_usd']:.4f}, max ${summary['max_cost_usd']:.4f}")
        n_mistakes = summary["invoices_with_money_mistakes"]
        print(
            f"  arithmetic check: caught {summary['mistakes_caught']} of {n_mistakes} invoice(s) with a money "
            f"mistake; held {summary['false_alarms']} of {summary['scored'] - n_mistakes} correct one(s) anyway"
        )
        if summary["failed"]:
            print(f"  extraction failed outright: {', '.join(summary['failed'])}")
    if args.fake:
        print("\nDRY RUN (--fake): the numbers above mean nothing")

    if not args.fake:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
