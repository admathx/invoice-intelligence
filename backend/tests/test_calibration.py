"""Calibration from real use (validation/calibration_report.py): the
statistics that decide whether a threshold may move."""
from decimal import Decimal

from validation.calibration_report import calibrate_extraction, calibrate_matching, wilson_interval

CONFIRMED = "invoice_line.match_confirmed"
CORRECTED = "invoice_line.match_corrected"
AUTO, LOW, CEILING = Decimal("0.92"), Decimal("0.80"), 0.01


def _verdicts(confidence: str, confirmed: int, corrected: int) -> list:
    c = Decimal(confidence)
    return [(CONFIRMED, c)] * confirmed + [(CORRECTED, c)] * corrected


def test_zero_errors_in_a_small_sample_is_not_a_zero_error_rate():
    low, high = wilson_interval(0, 20)
    assert low == 0.0 and high > 0.15
    # It takes 381 clean decisions for the upper bound to clear 1%.
    assert wilson_interval(0, 380)[1] > 0.01 >= wilson_interval(0, 381)[1]


def test_verdicts_land_in_the_band_of_the_confidence_they_were_made_at():
    result = calibrate_matching(
        _verdicts("0.81", 3, 1) + _verdicts("0.915", 5, 0) + [(CORRECTED, None)], 0, 0, AUTO, LOW, CEILING
    )
    by_band = {(b.low, b.high): (b.confirmed, b.corrected) for b in result.bands if b.total}
    assert by_band == {(0.80, 0.85): (3, 1), (0.90, 0.92): (5, 0)}
    assert result.no_suggestion == 1


def test_auto_accept_can_move_down_only_on_enough_evidence():
    thin = calibrate_matching(_verdicts("0.91", 50, 0), 0, 0, AUTO, LOW, CEILING)
    assert thin.suggestions == []

    ample = calibrate_matching(_verdicts("0.91", 400, 0) + _verdicts("0.89", 400, 0), 0, 0, AUTO, LOW, CEILING)
    assert any("could start at 0.88" in s for s in ample.suggestions)

    # A gap in the evidence stops the walk: 0.88-0.90 is proven, but the band
    # between it and the threshold isn't, so nothing moves.
    gapped = calibrate_matching(_verdicts("0.91", 30, 0) + _verdicts("0.89", 400, 0), 0, 0, AUTO, LOW, CEILING)
    assert not any("could start" in s for s in gapped.suggestions)


def test_disputed_auto_matches_flag_a_threshold_that_is_too_loose():
    fine = calibrate_matching([], auto_lines=10_000, auto_disputed=20, auto_threshold=AUTO, review_low=LOW, ceiling=CEILING)
    assert not fine.too_loose
    loose = calibrate_matching([], auto_lines=1_000, auto_disputed=40, auto_threshold=AUTO, review_low=LOW, ceiling=CEILING)
    assert loose.too_loose and any("RAISE" in s for s in loose.suggestions)


def test_mostly_wrong_suggestions_at_the_floor_are_called_out():
    result = calibrate_matching(_verdicts("0.82", 10, 40), 0, 0, AUTO, LOW, CEILING)
    assert any("review_queue_confidence_low" in s for s in result.suggestions)


def test_extraction_corrections_are_counted_per_misread_not_per_edit():
    events = {
        "inv-1": [
            {"action": "invoice.edited", "details": {"line_changes": {"1": {"unit_price": {"from": "74.50", "to": "47.50"}}}}},
            # The same field on the same line fixed again: still one misreading.
            {"action": "invoice.edited", "details": {"line_changes": {"1": {"unit_price": {"from": "47.50", "to": "47.05"}}}}},
            {"action": "invoice.edited", "details": {"changes": {"total": {"from": "1", "to": "2"}}}},
            {"action": "invoice_line.added", "details": {}},
            {"action": "invoice.confirmed", "details": {"line_count": 4}},
        ],
        # Held, then confirmed with nothing changed: a false alarm.
        "inv-2": [{"action": "invoice.confirmed", "details": {"line_count": 7}}],
        # Never confirmed: nobody compared it to the page, so it doesn't count.
        "inv-3": [{"action": "invoice.edited", "details": {"changes": {"tax": {"from": "0", "to": "1"}}}}],
    }
    result = calibrate_extraction(events)
    assert result.reviewed_invoices == 2 and result.reviewed_lines == 11
    assert result.field_corrections == {"unit_price": 1}
    assert result.header_corrections == {"total": 1}
    assert result.lines_added == 1 and result.false_alarms == 1
