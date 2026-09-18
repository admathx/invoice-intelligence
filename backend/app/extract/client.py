from app.extract.schema import ExtractedInvoice, ExtractedLineItem

# Phase 0 walking-skeleton extractor: returns a fixed payload so the rest of the
# pipeline (persistence, API, frontend) can be built and tested without burning
# API calls or needing real invoices. Replaced by a real vision-model call in Phase 2
# (see app/extract/prompt.py and app/extract/confidence.py, not yet built).

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


class FakeExtractorClient:
    """Deterministic stand-in for the real vision-model extractor. Phase 0 only."""

    def extract(self, page_image_paths: list) -> tuple[ExtractedInvoice, float]:
        """Returns (extracted_invoice, cost_usd)."""
        return FAKE_PAYLOAD, 0.0
