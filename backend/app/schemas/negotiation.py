import uuid
from decimal import Decimal

from pydantic import BaseModel


class NegotiationLineOut(BaseModel):
    canonical_sku_id: uuid.UUID
    canonical_sku_name: str
    # "peer" (target is the peer p25) or "history" (target is the tenant's own
    # prior p25). Per line, not per sheet: with basis=auto each SKU uses the
    # strongest evidence it has, and the page labels each row accordingly —
    # two different claims under one unqualified "target price" header is how
    # a rep finds the one line that doesn't hold up.
    basis: str
    current_price: Decimal
    current_price_line_item_id: uuid.UUID
    target_price: Decimal
    peer_account_count: int | None  # set on peer lines: how many independent businesses
    history_observation_count: int | None  # set on history lines: how many prior purchases
    trailing_quantity: Decimal
    quantity_line_item_ids: list[uuid.UUID]
    recoverable_in_window: Decimal
    annualized_savings: Decimal


class NegotiationSheetOut(BaseModel):
    lines: list[NegotiationLineOut]
    # The evidence base behind every dollar figure above: how many days of
    # invoices the sheet saw, and what those were multiplied by to reach a
    # yearly number. Exposed rather than assumed so the page can say so.
    window_days: int
    annualization_factor: Decimal
    # Summed server-side in Decimal — SPEC.md §11's money discipline applies
    # just as much to a total shown on the page as to any per-line figure;
    # a client-side sum over stringified Decimals via JS floats risks a
    # rounding drift on the one document a sales rep is meant to scrutinize.
    total_annualized_savings: Decimal
