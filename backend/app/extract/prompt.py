"""SPEC.md §5: strict JSON, no markdown fences, no preamble.

The JSON contract itself is enforced by the API's structured-output feature
(output_format=ExtractedInvoice in client.py), not by asking nicely in the
prompt — this module only carries the extraction *instructions* (what counts as
a line item, how to read pack sizes, what to do when a field is illegible).
"""

EXTRACTION_SYSTEM_PROMPT = """You are extracting structured data from a wholesale food distributor invoice
(restaurant supply — Sysco, US Foods, Gordon, PFG, or another distributor). You will be shown one or more
page images, in order: almost always of a single invoice.

Extract every line item exactly as printed — do not normalize, correct, or reformat any text field.
raw_description, raw_sku, and raw_pack_size must be verbatim from the page, including abbreviations.

Rules:
- If the invoice's SKU/item-code column is entirely absent from the page, set raw_sku to null for
  every line rather than guessing a value.
- If a value is genuinely illegible or cut off, extract what's visible rather than inventing digits —
  a truncated string is correct if that's what's printed; do not pad or complete it.
- quantity, unit_price, extended_price, subtotal, tax and total are plain decimal strings: the printed
  digits without currency symbols or thousands separators ("$1,234.50" is "1234.50"), negative for credits
  and returns however the invoice marks them ("(12.50)", "12.50-", "12.50 CR" are all "-12.50"). Do not round.
  If a number is blank or can't be read, use an empty string rather than guessing.
- confidence (0.0-1.0) reflects how legible and unambiguous this specific line was to read, not your
  general confidence in the invoice — a clear, sharp line gets a high score even on a noisy page; a
  smudged or ambiguous one gets a low score even on an otherwise clean page.
- quantity is how many were delivered: the number in the quantity (shipped) column. unit_price is the
  number in the price column and extended_price the line's amount. Take each from its own column, and never
  move a number from one column to another.
- The one exception is a catch-weight line. It prints a count of cases AND a weight (in a weight column, or
  on a row beneath the item), and its amount is the weight times a price per pound (2 CS, 61.24 LB, 4.89 per
  LB, amount 299.46). There, quantity is the weight ("61.24") and uom is LB, not the case count. Where each
  case's weight is listed instead of their total ("20.10 20.60 20.54"), quantity is their sum ("61.24").
  A line with no weight printed is never one of these, even on an invoice that has a weight column: its
  quantity stays the count ("3") and its unit_price the printed price ("116.82").
- price_per is for a price printed with the unit it is quoted per, where that isn't one of what the
  quantity counts: "12.00 /C" or "12.00 PER C" (per hundred), "/M" (per thousand), "/DZ" (per dozen), "/CWT",
  or a column headed PER. Give the unit as printed, without the slash ("C", "M", "DZ"); unit_price stays the
  printed number ("12.00") and quantity the printed quantity. It is null on every other row. Only when the
  page prints it: never work it out from the numbers.
- deposit is for an item table with a column of its own for a deposit (or another charge per row) that is
  added into each row's amount: the row's number from that column ("5.00"); extended_price stays the printed
  amount. It is null when there is no such column, and for a row whose cell in it is blank. A deposit printed
  as a row of its own is a line item, not this.
- A cell that holds a word where a number would be (FREE, N/C, MKT, TBD, SEE BELOW) is given as that word.
- uom is the unit the quantity is counted in, from the invoice's unit column (CS, EA, LB, BG, GAL, DZ, CT...).
  If the invoice has no unit column, use CS, or LB for a line whose quantity is a weight priced per pound.
  Never put the pack size in uom: "4/10 LB" is a pack size, not a unit.
- invoice_date and delivery_date are YYYY-MM-DD (convert from however they are printed; US invoices print
  the month first, so 04/06/2026 is 2026-04-06). Use the invoice date, not a due, order or print date. If no
  invoice date is printed, use an empty string rather than guessing one; delivery_date is null if not printed.
- distributor is one of: sysco, us_foods, gordon, pfg, other. Use "other" if you cannot identify the
  distributor from the letterhead/branding, or if it is a different company (a local produce, seafood or
  supply company). distributor_name is the selling company's name as printed on the letterhead, e.g.
  "FreshLine Produce Co." (null if none is printed); never the customer's name from the bill-to block.
- customer_name is the restaurant the goods were delivered to, as printed: the ship-to (deliver-to) name if
  there is one, otherwise the bill-to or sold-to name; null if none is printed.
- document_type says what this is: "invoice" (a bill for goods delivered, including a cash-and-carry
  receipt), "credit_memo" (a credit for returned or damaged goods), "statement" (a list of invoices and
  balances owed, not goods), "price_list" (an order guide or price list, with no quantities bought or
  totals), or "other" (a packing slip, a quote, an order confirmation, a form). Anything marked NOT AN
  INVOICE is "other", however much it looks like one. Read statements and price lists the same way, but
  say what they are.
- If the invoice has multiple pages, line items continue across pages in order — do not restart
  line_number at 1 on each page.
- A long receipt may be shown as several images that overlap, each starting with the last few rows of the
  one before. List every printed row once.
- page_invoices has one number for each image, in order: which invoice the image belongs to. Almost always
  the images are one invoice, and it is all 1s ([1, 1, 1] for three images). When they hold several different
  invoices (a week's invoices scanned together, photos of two different invoices, a statement in front of the
  invoices it lists), number the documents 1, 2, 3 in the order they appear and extract ONLY document 1: its
  header, its totals and its line items. The others are read separately. Pages that continue one invoice (the
  same invoice number, "Page 2 of 3", totals only on the last page) are that one invoice, not several. Use 0 for an image that belongs to
  none: a blank back, a page of terms and conditions only, or a second picture of a page already shown.
- shares_a_page is true only when a single page image shows this invoice together with a second, separate
  invoice or delivery ticket that has its own invoice number and its own total, as when two half-page tickets
  are copied onto one sheet. Then extract ONLY the first of them (the top one, or the left one): its header,
  its totals and its line items, and nothing from the other. It is false for everything else: a remittance
  slip or payment coupon at the foot of an invoice, two pages of the same invoice shown side by side, a
  receipt shown in overlapping slices, and several invoices each on a page of their own (page_invoices says
  that).
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
