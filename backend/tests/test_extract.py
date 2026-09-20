"""Phase 2 gate, per SPEC.md §10: arithmetic validation, review routing, the
retry-once-on-validation-failure contract, and cost accounting. Everything here
is mocked — no ANTHROPIC_API_KEY or network access needed, and no API spend.
"""
import json
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.extract.client import AnthropicExtractorClient, ExtractionFailedError, FakeExtractorClient, cost_usd, get_extractor
from app.extract.confidence import assess_extraction
from app.extract.schema import ExtractedInvoice, ExtractedLineItem
from app.models.enums import InvoiceStatus


def _line(**overrides) -> ExtractedLineItem:
    defaults = dict(
        line_number=1,
        raw_sku="1234567",
        raw_description="TEST ITEM",
        raw_pack_size="4/5 LB",
        quantity="2",
        uom="CS",
        unit_price="10.0000",
        extended_price="20.0000",
        confidence=0.95,
    )
    defaults.update(overrides)
    return ExtractedLineItem(**defaults)


def _invoice(lines: list[ExtractedLineItem], **overrides) -> ExtractedInvoice:
    subtotal = sum((float(li.extended_price) for li in lines))
    defaults = dict(
        distributor="sysco",
        invoice_number="INV-1",
        invoice_date="2026-01-01",
        delivery_date=None,
        subtotal=f"{subtotal:.4f}",
        tax="0.0000",
        total=f"{subtotal:.4f}",
        line_items=lines,
    )
    defaults.update(overrides)
    return ExtractedInvoice(**defaults)


# --- Arithmetic validation / review routing (app.extract.confidence) ---


def test_clean_invoice_routes_to_extracted():
    invoice = _invoice([_line()])
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.extracted
    assert assessment.reasons == []


def test_line_arithmetic_mismatch_routes_to_needs_review():
    # 2 * 10.00 = 20.00, not 25.00
    invoice = _invoice([_line(extended_price="25.0000")])
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.failed_line_numbers == [1]


def test_low_confidence_line_routes_to_needs_review():
    invoice = _invoice([_line(confidence=0.80)])
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.low_confidence_line_numbers == [1]


def test_confidence_exactly_at_threshold_is_not_low():
    invoice = _invoice([_line(confidence=0.85)])
    assessment = assess_extraction(invoice)
    assert assessment.low_confidence_line_numbers == []
    assert assessment.status == InvoiceStatus.extracted


def test_lines_not_summing_to_subtotal_routes_to_needs_review():
    invoice = _invoice([_line()], subtotal="99.0000", total="99.0000")
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.lines_sum_to_subtotal is False


def test_subtotal_plus_tax_not_reconciling_routes_to_needs_review():
    invoice = _invoice([_line()], tax="1.0000", total="20.0000")  # should be 21.00
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.totals_reconcile is False


def test_empty_line_items_routes_to_needs_review():
    # Every arithmetic check passes vacuously on an empty invoice (the loop
    # never runs, all([]) is True, sum([]) == 0 reconciles against zeroed
    # totals), so this needs its own explicit check — a blank/unreadable
    # page must not ship as `extracted`.
    invoice = _invoice([], subtotal="0.0000", tax="0.0000", total="0.0000")
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.no_line_items is True
    assert "no line items extracted" in assessment.reasons


def test_distributor_other_routes_to_needs_review():
    invoice = _invoice([_line()], distributor="other")
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.distributor_is_other is True


def test_unparseable_decimal_counts_as_line_failure_not_a_crash():
    invoice = _invoice([_line(unit_price="not-a-number")])
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.failed_line_numbers == [1]


def test_within_a_cent_tolerance_is_not_a_failure():
    # Real invoices round to the cent; a sub-cent drift from the model's own
    # arithmetic must not trip the gate.
    invoice = _invoice([_line(quantity="3", unit_price="12.3333", extended_price="37.0000")])
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.extracted


# --- cost_usd ---


def test_cost_usd_matches_published_rate():
    usage = SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-sonnet-4-6", usage) == pytest.approx(3.00 + 15.00)


def test_cost_usd_unknown_model_raises():
    usage = SimpleNamespace(input_tokens=100, output_tokens=100)
    with pytest.raises(ValueError):
        cost_usd("some-unpriced-model", usage)


# --- get_extractor selection ---


def test_get_extractor_picks_fake_without_key(monkeypatch):
    monkeypatch.setattr("app.extract.client.settings.anthropic_api_key", "")
    assert isinstance(get_extractor(), FakeExtractorClient)


def test_get_extractor_picks_real_with_key(monkeypatch):
    monkeypatch.setattr("app.extract.client.settings.anthropic_api_key", "sk-ant-test-key")
    assert isinstance(get_extractor(), AnthropicExtractorClient)


# --- Retry-once-on-validation-failure (app.extract.client.AnthropicExtractorClient) ---


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeResponse:
    content: list
    usage: _FakeUsage


def _valid_payload_text() -> str:
    invoice = _invoice([_line()])
    return json.dumps(json.loads(invoice.model_dump_json()))


class _StubMessages:
    """Stands in for client.messages, returning queued responses in order."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


@pytest.fixture()
def extractor_client(monkeypatch):
    monkeypatch.setattr("app.extract.client.settings.anthropic_api_key", "sk-ant-test-key")
    client = AnthropicExtractorClient()
    return client


def test_extract_succeeds_on_first_attempt_single_call(extractor_client):
    resp = _FakeResponse(content=[_FakeTextBlock(_valid_payload_text())], usage=_FakeUsage(1000, 500))
    stub = _StubMessages([resp])
    extractor_client.client.messages = stub

    extracted, cost = extractor_client.extract([])

    assert len(stub.calls) == 1
    assert extracted.line_items[0].raw_description == "TEST ITEM"
    assert cost == pytest.approx(cost_usd("claude-sonnet-4-6", resp.usage))


def test_extract_retries_once_then_succeeds(extractor_client):
    bad = _FakeResponse(content=[_FakeTextBlock("not valid json{{{")], usage=_FakeUsage(1000, 500))
    good = _FakeResponse(content=[_FakeTextBlock(_valid_payload_text())], usage=_FakeUsage(1200, 600))
    stub = _StubMessages([bad, good])
    extractor_client.client.messages = stub

    extracted, cost = extractor_client.extract([])

    assert len(stub.calls) == 2
    # The retry prompt must reference the validation error and be appended as
    # new turns, not replace the original request.
    second_call_messages = stub.calls[1]["messages"]
    assert len(second_call_messages) == 3  # original user turn + assistant + retry user turn
    assert extracted.line_items[0].raw_description == "TEST ITEM"
    # Cost accumulates across both attempts — the failed first attempt still
    # spent tokens and SPEC.md's conventions require logging every model call.
    expected_cost = cost_usd("claude-sonnet-4-6", bad.usage) + cost_usd("claude-sonnet-4-6", good.usage)
    assert cost == pytest.approx(expected_cost)


def test_extract_fails_after_one_retry_raises_extraction_failed(extractor_client):
    bad1 = _FakeResponse(content=[_FakeTextBlock("not valid json{{{")], usage=_FakeUsage(1000, 500))
    bad2 = _FakeResponse(content=[_FakeTextBlock("still not valid")], usage=_FakeUsage(1000, 500))
    stub = _StubMessages([bad1, bad2])
    extractor_client.client.messages = stub

    with pytest.raises(ExtractionFailedError):
        extractor_client.extract([])

    # Exactly one retry, per SPEC.md §5 — not an unbounded loop.
    assert len(stub.calls) == 2


def test_extract_schema_response_format_is_json_schema(extractor_client):
    resp = _FakeResponse(content=[_FakeTextBlock(_valid_payload_text())], usage=_FakeUsage(100, 50))
    stub = _StubMessages([resp])
    extractor_client.client.messages = stub

    extractor_client.extract([])

    output_config = stub.calls[0]["output_config"]
    assert output_config["format"]["type"] == "json_schema"
    assert output_config["format"]["schema"]["additionalProperties"] is False
