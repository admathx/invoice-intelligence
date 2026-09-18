"""SPEC.md §5: strict JSON, no markdown fences, no preamble.

The JSON contract itself is enforced by the API's structured-output feature
(output_format=ExtractedInvoice in client.py), not by asking nicely in the
prompt — this module only carries the extraction *instructions* (what counts as
a line item, how to read pack sizes, what to do when a field is illegible).
"""

EXTRACTION_SYSTEM_PROMPT = """You are extracting structured data from a wholesale food distributor invoice
(restaurant supply — Sysco, US Foods, Gordon, PFG, or another distributor). You will be shown one or more
page images of a single invoice, in order.

Extract every line item exactly as printed — do not normalize, correct, or reformat any text field.
raw_description, raw_sku, and raw_pack_size must be verbatim from the page, including abbreviations.

Rules:
- If the invoice's SKU/item-code column is entirely absent from the page, set raw_sku to null for
  every line rather than guessing a value.
- If a value is genuinely illegible or cut off, extract what's visible rather than inventing digits —
  a truncated string is correct if that's what's printed; do not pad or complete it.
- quantity, unit_price, and extended_price are decimal strings (e.g. "12.5000"), not numbers — copy
  the printed digits, do not round or reformat.
- confidence (0.0-1.0) reflects how legible and unambiguous this specific line was to read, not your
  general confidence in the invoice — a clear, sharp line gets a high score even on a noisy page; a
  smudged or ambiguous one gets a low score even on an otherwise clean page.
- distributor is one of: sysco, us_foods, gordon, pfg, other. Use "other" if you cannot identify the
  distributor from the letterhead/branding.
- If the invoice has multiple pages, line items continue across pages in order — do not restart
  line_number at 1 on each page.
"""


def build_retry_prompt(validation_error: str) -> str:
    """Appended as an additional user-turn instruction when the first attempt's
    output didn't validate against the schema. Per SPEC.md §5: retry once with
    the validation error appended, then give up.
    """
    return (
        "Your previous response did not match the required schema. "
        f"Validation error:\n{validation_error}\n\n"
        "Re-extract the same invoice, correcting the fields that caused this error. "
        "Follow the schema exactly."
    )
