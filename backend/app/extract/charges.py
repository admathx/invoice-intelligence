"""Fees, surcharges, deposits and discounts on an invoice.

They're real money on the invoice (and count in its totals and spending),
but nothing was bought: there's no product to match and no price to track.
On a realistic test set, "FUEL SURCHARGE", "DELIVERY FEE" and "VOLUME
DISCOUNT 2%" sat on Match items with nothing a person could match them to.

Recognized by wording, and only on lines with no pack size printed: a
product line always has one, and "DELIVERY" or "FREIGHT" in a real item's
name then can't mislead it. A person can mark any other line the same way on
Match items.
"""
import re

# Words that only ever name a charge. Not "DELIVERY" or "DISC" alone: an
# insulated pizza delivery bag or a sanding disc is something bought
# ("DELIVERY FEE" and "DELIVERY CHARGE" are still caught).
_CHARGE_WORDS = re.compile(
    r"\b(FEE|FEES|SURCHARGE|SURCHG|DISCOUNT|DEPOSIT|FREIGHT|SHIPPING|HANDLING|RESTOCK|RESTOCKING|"
    r"REBATE|ALLOWANCE|ROUNDING|GRATUITY|MINIMUM ORDER|SERVICE CHARGE|DELIVERY CHARGE)\b"
)


def is_charge(description: str | None, raw_pack_size: str | None) -> bool:
    if (raw_pack_size or "").strip():
        return False
    return bool(_CHARGE_WORDS.search((description or "").upper()))
