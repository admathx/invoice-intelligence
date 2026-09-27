"""Calibration from real use: python -m validation.calibration_report

The thresholds in thresholds.yaml were tuned on the synthetic corpus. Real
invoices are the test that matters, and the people using the app are
already producing the labels: every review-queue confirm or correct is a
verdict on a match suggestion made at a known confidence, and every invoice
correction records what extraction read next to what the page said. This
reads those verdicts from the audit trail and reports:

1. Matching. For each confidence band, how often a person agreed with the
   suggestion. The auto-accept threshold (auto_match_confidence_threshold)
   should sit where suggestions are right often enough to skip review; the
   review floor (review_queue_confidence_low) where they're still worth
   showing. Plus how often an auto-accepted match was later disputed.
2. Extraction. On invoices a person reviewed and confirmed: which fields they
   had to correct, lines they added (extraction missed them) or removed (it
   invented them), and how often the "doesn't add up" hold was a false alarm.

Suggestions are made only when the evidence carries them. "Suggestions at
0.90 were right 50 times out of 50" does not show an error rate under the 1%
false-match ceiling; with no errors at all it takes 381 decisions for the
95% upper bound to get under 1%, so a band with fewer can't move a threshold.

Exits non-zero when the evidence says the current auto-accept threshold is
too loose (its disputed rate is above the ceiling even at the most generous
reading), which is the one finding that means wrong prices are reaching
benchmarks today. Reads the database; costs nothing.
"""
import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import AuditEvent, InvoiceLineItem  # noqa: E402
from app.models.enums import ReviewStatus  # noqa: E402

THRESHOLDS_PATH = REPO_ROOT / "validation" / "thresholds.yaml"
RESULTS_PATH = REPO_ROOT / "validation" / "results" / "calibration.json"

# Confidence bands the review queue's range is split into. Each has to earn a
# threshold move on its own evidence.
BANDS = [
    (Decimal("0.00"), Decimal("0.80")),
    (Decimal("0.80"), Decimal("0.85")),
    (Decimal("0.85"), Decimal("0.88")),
    (Decimal("0.88"), Decimal("0.90")),
    (Decimal("0.90"), Decimal("0.92")),
    (Decimal("0.92"), Decimal("0.95")),
    (Decimal("0.95"), Decimal("1.01")),
]
Z_95 = 1.96


def wilson_interval(errors: int, total: int, z: float = Z_95) -> tuple[float, float]:
    """95% interval for an error rate from `errors` out of `total`. Wilson,
    not the naive p +/- z*sqrt(p(1-p)/n), which claims certainty from zero
    observed errors: 0 of 20 is not a 0% error rate."""
    if total == 0:
        return (0.0, 1.0)
    p = errors / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass
class Band:
    low: float
    high: float
    confirmed: int = 0
    corrected: int = 0

    @property
    def total(self) -> int:
        return self.confirmed + self.corrected

    @property
    def wrong_rate(self) -> float | None:
        return self.corrected / self.total if self.total else None

    def interval(self) -> tuple[float, float]:
        return wilson_interval(self.corrected, self.total)


@dataclass
class MatchingCalibration:
    bands: list[Band]
    no_suggestion: int
    auto_lines: int
    auto_disputed: int
    auto_threshold: float
    review_low: float
    ceiling: float
    suggestions: list[str] = field(default_factory=list)
    too_loose: bool = False


def calibrate_matching(
    decisions: list[tuple[str, Decimal | None]],
    auto_lines: int,
    auto_disputed: int,
    auto_threshold: Decimal,
    review_low: Decimal,
    ceiling: float,
) -> MatchingCalibration:
    """decisions: (action, confidence the matcher had) per review verdict,
    action being "invoice_line.match_confirmed" or "...match_corrected"."""
    bands = [Band(float(lo), float(hi)) for lo, hi in BANDS]
    no_suggestion = 0
    for action, confidence in decisions:
        if confidence is None:
            # Nothing was suggested; the person picked from scratch. Says
            # nothing about the matcher's calibration.
            no_suggestion += 1
            continue
        band = next(b for b, (lo, hi) in zip(bands, BANDS) if lo <= confidence < hi)
        if action.endswith("match_confirmed"):
            band.confirmed += 1
        else:
            band.corrected += 1

    result = MatchingCalibration(
        bands=bands,
        no_suggestion=no_suggestion,
        auto_lines=auto_lines,
        auto_disputed=auto_disputed,
        auto_threshold=float(auto_threshold),
        review_low=float(review_low),
        ceiling=ceiling,
    )

    # Too loose: even the most generous reading of the auto tier's disputes
    # exceeds the ceiling. Disputes undercount errors (nobody checks most
    # auto-accepted lines), so this can only ever understate the problem.
    if auto_lines:
        disputed_low, _ = wilson_interval(auto_disputed, auto_lines)
        if disputed_low > ceiling:
            result.too_loose = True
            result.suggestions.append(
                f"RAISE the auto-accept threshold: {auto_disputed} of {auto_lines} auto-accepted matches were "
                f"disputed, at least {disputed_low:.1%} even at the most generous reading, over the "
                f"{ceiling:.0%} ceiling."
            )

    # Could auto-accept start lower? Walk down from the threshold through the
    # contiguous bands whose error rate is shown to be under the ceiling.
    lowest_safe = None
    for band in sorted(
        (b for b in bands if b.high <= float(auto_threshold) + 1e-9 and b.low >= float(review_low) - 1e-9),
        key=lambda b: b.low,
        reverse=True,
    ):
        _, upper = band.interval()
        if band.total and upper <= ceiling:
            lowest_safe = band.low
        else:
            break
    if lowest_safe is not None:
        result.suggestions.append(
            f"Auto-accept could start at {lowest_safe:.2f} instead of {float(auto_threshold):.2f}: suggestions "
            f"from there up were confirmed often enough that the error rate is under {ceiling:.0%} at 95% confidence."
        )

    # Is the review floor showing people suggestions that are mostly wrong?
    floor_band = next((b for b in bands if abs(b.low - float(review_low)) < 1e-9), None)
    if floor_band and floor_band.total >= 30:
        low, _ = floor_band.interval()
        if low > 0.5:
            result.suggestions.append(
                f"Suggestions just above the review floor ({floor_band.low:.2f}-{floor_band.high:.2f}) were wrong "
                f"{floor_band.wrong_rate:.0%} of the time: consider raising review_queue_confidence_low so people "
                f"search from scratch instead of rejecting a bad guess first."
            )
    return result


@dataclass
class ExtractionCalibration:
    reviewed_invoices: int = 0
    reviewed_lines: int = 0
    false_alarms: int = 0
    field_corrections: dict[str, int] = field(default_factory=dict)
    header_corrections: dict[str, int] = field(default_factory=dict)
    lines_added: int = 0
    lines_removed: int = 0


def calibrate_extraction(events_by_invoice: dict[str, list[dict]]) -> ExtractionCalibration:
    """events_by_invoice: each confirmed invoice's audit events, as dicts with
    "action" and "details". Only invoices a person confirmed count: those are
    the ones someone compared to the page."""
    result = ExtractionCalibration()
    fields: Counter = Counter()
    headers: Counter = Counter()
    for events in events_by_invoice.values():
        confirmed = [e for e in events if e["action"] == "invoice.confirmed"]
        if not confirmed:
            continue
        result.reviewed_invoices += 1
        result.reviewed_lines += int(confirmed[-1]["details"].get("line_count", 0))
        # A field corrected twice on one line is one misreading, not two.
        corrected_line_fields: set[tuple[str, str]] = set()
        corrected_headers: set[str] = set()
        touched = False
        for event in events:
            details = event["details"]
            if event["action"] == "invoice.edited":
                for name in details.get("changes", {}):
                    corrected_headers.add(name)
                    touched = True
                for line_number, changes in details.get("line_changes", {}).items():
                    for name in changes:
                        corrected_line_fields.add((line_number, name))
                        touched = True
            elif event["action"] == "invoice_line.added":
                result.lines_added += 1
                touched = True
            elif event["action"] == "invoice_line.removed":
                result.lines_removed += 1
                touched = True
        for _, name in corrected_line_fields:
            fields[name] += 1
        for name in corrected_headers:
            headers[name] += 1
        if not touched:
            # Held for review, confirmed without changing anything.
            result.false_alarms += 1
    result.field_corrections = dict(fields.most_common())
    result.header_corrections = dict(headers.most_common())
    return result


def _load(db) -> tuple[list, int, int, dict]:
    decisions: list[tuple[str, Decimal | None]] = []
    for action, details in db.execute(
        select(AuditEvent.action, AuditEvent.details).where(
            AuditEvent.action.in_(["invoice_line.match_confirmed", "invoice_line.match_corrected"])
        )
    ).all():
        if "suggested_confidence" in details:
            value = details["suggested_confidence"]
            # "none": nothing suggested, or suggested without a score.
            decisions.append((action, None if value == "none" else Decimal(str(value))))
        elif (details.get("canonical_sku") or {}).get("from") is None:
            # Nothing was suggested (a None confidence isn't stored).
            decisions.append((action, None))
        # Otherwise a verdict from before confidence was recorded: it can't be
        # placed in a band, so it's left out rather than guessed at.

    auto_lines = db.scalar(
        select(func.count(InvoiceLineItem.id))
        .where(InvoiceLineItem.review_status == ReviewStatus.auto)
        .execution_options(tenant_scope_bypass=True)
    )
    auto_disputed = db.scalar(
        select(func.count(AuditEvent.id)).where(
            AuditEvent.action == "invoice_line.reopened",
            AuditEvent.details["review_status"]["from"].astext == ReviewStatus.auto.value,
        )
    )

    events_by_invoice: dict[str, list[dict]] = defaultdict(list)
    for entity_id, action, details in db.execute(
        select(AuditEvent.entity_id, AuditEvent.action, AuditEvent.details)
        .where(AuditEvent.action.in_(["invoice.confirmed", "invoice.edited", "invoice_line.added", "invoice_line.removed"]))
        .order_by(AuditEvent.occurred_at)
    ).all():
        # Line events name their invoice in details; invoice events are about it.
        invoice_id = str(entity_id) if action.startswith("invoice.") else details.get("invoice_id")
        if invoice_id:
            events_by_invoice[invoice_id].append({"action": action, "details": details})
    return decisions, auto_lines, auto_disputed, events_by_invoice


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    thresholds = yaml.safe_load(THRESHOLDS_PATH.read_text())["phase3_normalization"]
    db = SessionLocal()
    try:
        decisions, auto_lines, auto_disputed, events_by_invoice = _load(db)
    finally:
        db.close()

    matching = calibrate_matching(
        decisions,
        auto_lines,
        auto_disputed,
        Decimal(str(thresholds["auto_match_confidence_threshold"])),
        Decimal(str(thresholds["review_queue_confidence_low"])),
        float(thresholds["false_match_rate_max"]),
    )
    extraction = calibrate_extraction(events_by_invoice)

    print("Matching: human verdicts on suggestions, by matcher confidence")
    print(f"  {'band':<12}{'confirmed':>10}{'corrected':>10}{'wrong':>8}   95% range")
    for band in matching.bands:
        if not band.total:
            continue
        low, high = band.interval()
        print(
            f"  {band.low:.2f}-{min(band.high, 1.0):.2f}  {band.confirmed:>10}{band.corrected:>10}"
            f"{band.wrong_rate:>8.1%}   {low:.1%}-{high:.1%}"
        )
    print(f"  picked from scratch (no suggestion): {matching.no_suggestion}")
    print(f"  auto-accepted lines: {auto_lines}, later disputed: {auto_disputed}")

    print("\nExtraction: invoices a person reviewed and confirmed")
    print(f"  invoices {extraction.reviewed_invoices}, lines {extraction.reviewed_lines}")
    print(f"  held but nothing needed fixing: {extraction.false_alarms}")
    print(f"  lines added (missed): {extraction.lines_added}, removed (invented): {extraction.lines_removed}")
    for name, count in extraction.field_corrections.items():
        print(f"  line field corrected: {name:<16} {count}")
    for name, count in extraction.header_corrections.items():
        print(f"  invoice field corrected: {name:<13} {count}")

    print("\nSuggestions:")
    for suggestion in matching.suggestions or ["none: not enough evidence yet to move a threshold"]:
        print(f"  - {suggestion}")

    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(
            {
                "matching": {**asdict(matching), "bands": [asdict(b) for b in matching.bands]},
                "extraction": asdict(extraction),
            },
            indent=2,
        )
    )
    return 1 if matching.too_loose else 0


if __name__ == "__main__":
    sys.exit(main())
