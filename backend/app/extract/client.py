import base64
import json
from pathlib import Path
from typing import Any

import anthropic
import pydantic

from app.config import settings
from app.extract.prompt import EXTRACTION_SYSTEM_PROMPT, build_retry_prompt
from app.extract.schema import ExtractedInvoice, ExtractedLineItem

# $ per 1M tokens (input, output). SPEC.md §2 pins claude-sonnet-4-6 for extraction.
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
}

MAX_TOKENS = 8000


class ExtractionFailedError(Exception):
    """The model's output didn't validate against the schema, twice in a row (SPEC.md §5)."""


def _image_block(page_path: Path) -> dict[str, Any]:
    data = base64.standard_b64encode(Path(page_path).read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _strict_json_schema() -> dict[str, Any]:
    """ExtractedInvoice's schema with additionalProperties:false enforced at every
    object level (including nested $defs), for the API's structured-output constraint.
    """
    schema = ExtractedInvoice.model_json_schema()

    def _tighten(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
            for value in node.values():
                _tighten(value)
        elif isinstance(node, list):
            for item in node:
                _tighten(item)

    _tighten(schema)
    return schema


def cost_usd(model: str, usage: Any) -> float:
    if model not in MODEL_PRICING_PER_MTOK:
        raise ValueError(f"no pricing entry for model {model!r} — add one to MODEL_PRICING_PER_MTOK")
    input_rate, output_rate = MODEL_PRICING_PER_MTOK[model]
    return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / 1_000_000


class AnthropicExtractorClient:
    """Real extraction: Anthropic vision + structured JSON, per SPEC.md §5.

    Uses client.messages.create() with a manually-built output_config.format
    schema (not the messages.parse() convenience method): on a validation
    failure, .parse() raises pydantic.ValidationError with no access to the
    response it just received, which would silently lose that attempt's token
    usage — and SPEC.md's own conventions require logging cost on every model
    call, not just successful ones.
    """

    def __init__(self) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.extraction_model
        self._schema = _strict_json_schema()

    def extract(self, page_image_paths: list[Path]) -> tuple[ExtractedInvoice, float]:
        image_blocks = [_image_block(p) for p in page_image_paths]
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [*image_blocks, {"type": "text", "text": "Extract this invoice."}]}
        ]

        total_cost = 0.0
        last_error: Exception | None = None

        for attempt in range(2):  # one retry per SPEC.md §5
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=EXTRACTION_SYSTEM_PROMPT,
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": self._schema}},
            )
            total_cost += cost_usd(self.model, response.usage)

            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                data = json.loads(text)
                extracted = ExtractedInvoice.model_validate(data)
                return extracted, total_cost
            except (json.JSONDecodeError, pydantic.ValidationError) as e:
                last_error = e
                if attempt == 0:
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": build_retry_prompt(str(e))})

        raise ExtractionFailedError(f"extraction did not validate after retry: {last_error}") from last_error


class FakeExtractorClient:
    """Deterministic stand-in for the real vision-model extractor. Used when
    ANTHROPIC_API_KEY isn't configured (Phase 0 dev flow, and tests) so the rest
    of the pipeline works without burning API calls or needing a real key.
    """

    def extract(self, page_image_paths: list) -> tuple[ExtractedInvoice, float]:
        """Returns (extracted_invoice, cost_usd)."""
        return FAKE_PAYLOAD, 0.0


FAKE_PAYLOAD = ExtractedInvoice(
    distributor="sysco",
    invoice_number="FAKE-0001",
    invoice_date="2026-01-06",
    delivery_date="2026-01-07",
    subtotal="142.50",
    tax="0.00",
    total="142.50",
    line_items=[
        ExtractedLineItem(
            line_number=1,
            raw_sku="4001122",
            raw_description="MOZZ SHRD WHL MLK 4/5 LB",
            raw_pack_size="4/5 LB",
            quantity="2",
            uom="CS",
            unit_price="47.50",
            extended_price="95.00",
            confidence=0.98,
        ),
        ExtractedLineItem(
            line_number=2,
            raw_sku="8823311",
            raw_description="TOMATO ROMA 25 LB",
            raw_pack_size="25 LB",
            quantity="1",
            uom="CS",
            unit_price="47.50",
            extended_price="47.50",
            confidence=0.97,
        ),
    ],
)


def get_extractor() -> "AnthropicExtractorClient | FakeExtractorClient":
    """Real extractor once ANTHROPIC_API_KEY is configured; the Phase 0 fake
    otherwise, so local dev and tests work without a key or API spend.
    """
    if settings.anthropic_api_key:
        return AnthropicExtractorClient()
    return FakeExtractorClient()
