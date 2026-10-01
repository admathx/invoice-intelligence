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
    stop_reason: str = "end_turn"


def _valid_payload_text() -> str:
    invoice = _invoice([_line()])
    return json.dumps(json.loads(invoice.model_dump_json()))


class _StubStream:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self) -> _FakeResponse:
        return self._response


class _StubMessages:
    """Stands in for client.messages, returning queued responses in order
    through the same stream()/get_final_message() shape the extractor uses."""

    def __init__(self, responses: list[_FakeResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return _StubStream(self._responses.pop(0))


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


def test_a_truncated_answer_fails_fast_instead_of_retrying_into_the_same_cap(extractor_client):
    """The first real Phase 2 run truncated a large invoice's JSON at the
    output cap, then retried into the same cap. A second attempt can't fit
    what the first couldn't, so it only doubled the spend."""
    cut_off = _FakeResponse(
        content=[_FakeTextBlock('{"distributor": "sysco", "line_items": [{"raw_desc')],
        usage=_FakeUsage(3000, 64000),
        stop_reason="max_tokens",
    )
    stub = _StubMessages([cut_off])
    extractor_client.client.messages = stub

    with pytest.raises(ExtractionFailedError) as exc:
        extractor_client.extract([])

    assert len(stub.calls) == 1
    assert "max_tokens" in str(exc.value)
    assert exc.value.cost_usd == pytest.approx(cost_usd("claude-sonnet-4-6", cut_off.usage))


def test_the_output_cap_leaves_room_for_the_largest_invoices(extractor_client):
    """~150-180 output tokens per line; the corpus has invoices of 120 lines."""
    resp = _FakeResponse(content=[_FakeTextBlock(_valid_payload_text())], usage=_FakeUsage(100, 50))
    stub = _StubMessages([resp])
    extractor_client.client.messages = stub

    extractor_client.extract([])

    assert stub.calls[0]["max_tokens"] >= 180 * 120


# --- no subtotal, no tax line ------------------------------------------------


def test_with_no_subtotal_printed_the_items_are_checked_against_the_total():
    """A short invoice or a till receipt prints only a total; every one was held."""
    invoice = _invoice([_line()], subtotal="", tax="1.5000", total="21.5000")
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.extracted
    assert assessment.lines_sum_to_subtotal and assessment.totals_reconcile


def test_with_no_subtotal_a_total_the_items_do_not_reach_is_still_held():
    invoice = _invoice([_line()], subtotal="", tax="0.0000", total="25.0000")
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.totals_reconcile is False


def test_no_tax_line_is_no_tax():
    invoice = _invoice([_line()], tax="")
    assert assess_extraction(invoice).status == InvoiceStatus.extracted
    # But a tax that was printed and couldn't be read still fails: the total includes it.
    invoice = _invoice([_line()], tax="", total="21.5000")
    assert assess_extraction(invoice).status == InvoiceStatus.needs_review


def test_an_unreadable_line_total_is_not_excused_by_a_missing_subtotal():
    invoice = _invoice([_line()], subtotal="", total="20.0000")
    invoice.line_items[0].extended_price = ""
    assessment = assess_extraction(invoice)
    assert assessment.status == InvoiceStatus.needs_review
    assert assessment.lines_sum_to_subtotal is False


# --- The request's size (a scanned stack, a long receipt in slices) -----------

import anthropic  # noqa: E402
from PIL import Image  # noqa: E402

from app.extract import client as extract_client  # noqa: E402


def _scan_like_pages(tmp_path, n: int, size=(600, 800)) -> list:
    """Noisy pages, as a scan or photo renders: PNGs that barely compress."""
    paths = []
    for i in range(n):
        path = tmp_path / f"page_{i + 1:03d}.png"
        Image.effect_noise(size, 40).convert("RGB").save(path)
        paths.append(path)
    return paths


def _sizes(tmp_path):
    pages = _scan_like_pages(tmp_path, 3)
    png = extract_client._encoded_size([extract_client._image_block(p) for p in pages])
    jpeg = extract_client._encoded_size([extract_client._jpeg_block(p) for p in pages])
    assert jpeg < png
    return pages, png, jpeg


def test_pages_go_as_rendered_when_they_fit_together(tmp_path):
    pages, png, _ = _sizes(tmp_path)
    blocks = extract_client.image_blocks(pages, limit=png)
    assert {b["source"]["media_type"] for b in blocks} == {"image/png"}


def test_pages_too_big_together_as_png_go_as_jpeg(tmp_path):
    """About seventeen scanned pages passed what one request takes: refused,
    and reported as the service being down."""
    pages, png, jpeg = _sizes(tmp_path)
    blocks = extract_client.image_blocks(pages, limit=png - 1)
    assert {b["source"]["media_type"] for b in blocks} == {"image/jpeg"}
    assert extract_client._encoded_size(blocks) == jpeg


def test_pages_too_big_even_as_jpeg_are_the_invoices_problem_and_cost_nothing(tmp_path, extractor_client):
    pages, _, jpeg = _sizes(tmp_path)
    with pytest.raises(ExtractionFailedError, match="too much to read at once") as failure:
        extract_client.image_blocks(pages, limit=jpeg - 1)
    assert failure.value.cost_usd == 0.0

    # And the model is never called.
    extractor_client.client.messages = _StubMessages([])
    monkeypatch_limit = extract_client.MAX_IMAGE_BYTES
    extract_client.MAX_IMAGE_BYTES = jpeg - 1
    try:
        with pytest.raises(ExtractionFailedError):
            extractor_client.extract(pages)
    finally:
        extract_client.MAX_IMAGE_BYTES = monkeypatch_limit
    assert extractor_client.client.messages.calls == []


def test_a_request_refused_for_its_size_is_the_invoices_problem(extractor_client):
    """Whatever the estimate said: not a service outage, so no one is paged
    and the invoice is kept for a person."""

    class _TooLarge:
        calls = 0

        def stream(self, **kwargs):
            _TooLarge.calls += 1
            raise anthropic.RequestTooLargeError.__new__(anthropic.RequestTooLargeError)

    extractor_client.client.messages = _TooLarge()
    with pytest.raises(ExtractionFailedError, match="too large to read at once") as failure:
        extractor_client.extract([])
    assert failure.value.cost_usd == 0.0 and _TooLarge.calls == 1  # not retried


# --- Every page accounted for (a 24-page stack answered from its last two) -----


def _pages_response(page_invoices: list[int]) -> _FakeResponse:
    payload = json.loads(_valid_payload_text())
    payload["page_invoices"] = page_invoices
    return _FakeResponse(content=[_FakeTextBlock(json.dumps(payload))], usage=_FakeUsage(1000, 500))


def test_one_page_is_shown_as_it_always_was(tmp_path):
    (page,) = _scan_like_pages(tmp_path, 1, size=(60, 80))
    content = extract_client.page_content([page])
    assert [block["type"] for block in content] == ["image", "text"]
    assert content[-1]["text"] == "Extract this invoice."


def test_several_pages_are_each_introduced_and_counted(tmp_path):
    pages = _scan_like_pages(tmp_path, 3, size=(60, 80))
    content = extract_client.page_content(pages)
    assert [block["type"] for block in content] == ["text", "image"] * 3 + ["text"]
    assert [content[i]["text"] for i in (0, 2, 4)] == ["Page image 1 of 3:", "Page image 2 of 3:", "Page image 3 of 3:"]
    assert "exactly 3 numbers" in content[-1]["text"]


def test_a_reading_that_leaves_pages_out_is_asked_again_and_then_refused(tmp_path, extractor_client):
    """Twenty-four page images, page_invoices for two, and the last invoice's
    items: read as it stood, one invoice Ready and fifteen gone."""
    pages = _scan_like_pages(tmp_path, 3, size=(60, 80))
    stub = _StubMessages([_pages_response([1, 2]), _pages_response([1, 1, 2])])
    extractor_client.client.messages = stub
    extracted, _ = extractor_client.extract(pages)
    assert extracted.page_invoices == [1, 1, 2] and len(stub.calls) == 2
    assert "needs exactly 3" in stub.calls[1]["messages"][-1]["content"]

    stub = _StubMessages([_pages_response([1, 2]), _pages_response([1])])
    extractor_client.client.messages = stub
    with pytest.raises(ExtractionFailedError, match="1 numbers for 3 page images") as failure:
        extractor_client.extract(pages)
    assert failure.value.cost_usd > 0  # both attempts were billed
