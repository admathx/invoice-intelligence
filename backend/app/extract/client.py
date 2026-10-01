import base64
import io
import json
from pathlib import Path
from typing import Any

import anthropic
import pydantic
from PIL import Image

from app.config import settings
from app.extract.prompt import EXTRACTION_SYSTEM_PROMPT, build_retry_prompt
from app.extract.schema import ExtractedInvoice, ExtractedLineItem

# $ per 1M tokens (input, output). SPEC.md §2 pins claude-sonnet-4-6 for extraction.
MODEL_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
}

# Room for the largest invoices. Output grows with line count (every line is
# a JSON object of nine fields, ~150-180 tokens), and 8,000 truncated a
# ~120-line invoice mid-string during the first real Phase 2 run. The retry
# hit the same cap, so every large invoice would have been marked `failed`.
# Sonnet 4.6 allows up to 128K output; the SDK requires streaming for caps
# this large, which is why extract() streams. Only tokens actually generated
# are billed, so the higher cap costs nothing on small invoices.
MAX_TOKENS = 64000


class ExtractionFailedError(Exception):
    """The model's output didn't validate against the schema, twice in a row (SPEC.md §5).

    Carries what both attempts cost. They are real, billed calls, and in
    practice the most expensive invoices the system handles, since they are
    the hard-to-read ones. Raising without the cost would drop exactly the
    spend that SPEC.md's per-invoice cost gate most needs to see.
    """

    def __init__(self, message: str, cost_usd: float) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


# What the page images of one read may add up to, encoded. The API takes a
# request of up to 32 MB; this leaves room for the rest of it. A scanned or
# photographed page renders to a PNG of 1.1-1.5 MB, so about seventeen of
# them passed the limit: the request was refused, and because that wasn't
# an ExtractionFailedError it was reported as the service being down.
MAX_IMAGE_BYTES = 24 * 1024 * 1024
# The quality pages are re-encoded at when their PNGs are too much together:
# high enough that small print stays sharp.
JPEG_QUALITY = 90


def _block(data: bytes, media_type: str) -> dict[str, Any]:
    encoded = base64.standard_b64encode(data).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}}


def _image_block(page_path: Path) -> dict[str, Any]:
    return _block(Path(page_path).read_bytes(), "image/png")


def _jpeg_block(page_path: Path) -> dict[str, Any]:
    out = io.BytesIO()
    with Image.open(page_path) as image:
        image.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
    return _block(out.getvalue(), "image/jpeg")


class PagesUnaccountedFor(ValueError):
    """The reading says which invoice each page image belongs to for fewer
    (or more) images than it was shown."""


def _check_every_page_is_accounted_for(extracted: ExtractedInvoice, pages: int) -> None:
    """Shown a stack of 24 page images, the reader returned page_invoices
    for two and the items of the last invoice alone. Read as it stood, that
    was one invoice, Ready, and fifteen others gone without a word. A
    reading that doesn't say where every page went isn't one to trust."""
    if pages > 1 and len(extracted.page_invoices) != pages:
        raise PagesUnaccountedFor(
            f"page_invoices has {len(extracted.page_invoices)} numbers for {pages} page images; it needs exactly "
            f"{pages}, one for each image in order"
        )


def page_content(page_image_paths: list[Path]) -> list[dict[str, Any]]:
    """What the reader is shown: the page images, then the request. Several
    images are each introduced by which one it is, and the request says how
    many there were: without that, a long stack was answered from its last
    pages only. One image goes as it always has."""
    blocks = image_blocks(page_image_paths)
    if len(blocks) <= 1:
        return [*blocks, {"type": "text", "text": "Extract this invoice."}]
    count = len(blocks)
    content: list[dict[str, Any]] = []
    for number, block in enumerate(blocks, start=1):
        content += [{"type": "text", "text": f"Page image {number} of {count}:"}, block]
    content.append(
        {
            "type": "text",
            "text": (
                f"Extract this invoice. There are {count} page images above, so page_invoices has exactly {count} "
                "numbers, one for each image in order."
            ),
        }
    )
    return content


def _encoded_size(blocks: list[dict[str, Any]]) -> int:
    return sum(len(block["source"]["data"]) for block in blocks)


def image_blocks(page_image_paths: list[Path], limit: int | None = None) -> list[dict[str, Any]]:
    """The pages as the request carries them: as rendered (PNG) when they
    fit together, as JPEG when they don't. Raises ExtractionFailedError,
    with nothing spent, when even that is more than one request can hold:
    the invoice's problem (it's kept for a person), not the service's."""
    limit = MAX_IMAGE_BYTES if limit is None else limit
    blocks = [_image_block(p) for p in page_image_paths]
    if _encoded_size(blocks) <= limit:
        return blocks
    blocks = [_jpeg_block(p) for p in page_image_paths]
    if _encoded_size(blocks) <= limit:
        return blocks
    raise ExtractionFailedError(
        f"{len(page_image_paths)} pages are too much to read at once "
        f"({_encoded_size(blocks) // (1024 * 1024)} MB of page images; the most is {limit // (1024 * 1024)} MB)",
        cost_usd=0.0,
    )


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
        messages: list[dict[str, Any]] = [{"role": "user", "content": page_content(page_image_paths)}]

        total_cost = 0.0
        last_error: Exception | None = None

        for attempt in range(2):  # one retry per SPEC.md §5
            try:
                with self.client.messages.stream(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=EXTRACTION_SYSTEM_PROMPT,
                    messages=messages,
                    output_config={"format": {"type": "json_schema", "schema": self._schema}},
                ) as stream:
                    response = stream.get_final_message()
            except anthropic.RequestTooLargeError as exc:
                # Refused for its size whatever the estimate above said: about
                # this invoice, not the service, and trying again won't help.
                raise ExtractionFailedError(f"too large to read at once: {exc}", cost_usd=total_cost) from exc
            total_cost += cost_usd(self.model, response.usage)

            # Stop reasons a retry can't fix. A truncated answer at the cap
            # would be truncated at the same point again (asking to "fix" half
            # a document doesn't make room for the rest), and a refusal isn't a
            # formatting error. Retrying either just doubles the spend.
            if response.stop_reason in ("max_tokens", "refusal"):
                raise ExtractionFailedError(
                    f"extraction stopped with stop_reason={response.stop_reason!r} "
                    f"after {response.usage.output_tokens} output tokens",
                    cost_usd=total_cost,
                )

            text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                data = json.loads(text)
                extracted = ExtractedInvoice.model_validate(data)
                _check_every_page_is_accounted_for(extracted, len(page_image_paths))
                return extracted, total_cost
            except (json.JSONDecodeError, pydantic.ValidationError, PagesUnaccountedFor) as e:
                last_error = e
                if attempt == 0:
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": build_retry_prompt(str(e))})

        raise ExtractionFailedError(
            f"extraction did not validate after retry: {last_error}", cost_usd=total_cost
        ) from last_error


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
