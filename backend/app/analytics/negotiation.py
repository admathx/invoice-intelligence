"""SPEC.md §7: negotiation sheet — top 15 SKUs ranked by dollars recoverable,
not percentage gap ("a 3% gap on cheese beats a 40% gap on toothpicks").

Every line traces back to specific invoice_line_item_id rows (SPEC.md Phase 4
exit criteria): current_price comes from exactly one observation, and
quantity is the sum over an explicit, listed set of line items.

Two bases for the target price, because they become available at different
times in a customer's life:

- `peer`   — target is the peer p25 for the SKU's benchmark cell. The stronger
             argument, but it needs 5 distinct businesses buying that SKU in
             the same metro (or nationally), which a customer in a thin metro
             may not have for months, or ever.
- `history`— target is the median of the tenant's OWN prior prices for that
             SKU. Available from a customer's fourth delivery of an item with
             no peers at all, and it is the argument a distributor rep can
             least easily wave away: it is their own invoice from six weeks
             ago.

`auto` (the default) picks per SKU, preferring peer whenever the peer basis
has something to argue and falling back to history when it doesn't — either
because the cell is suppressed or because the tenant already beats peer p25
on that SKU while still paying well above what they used to. So the sheet is
non-empty on day one and each line strengthens independently as benchmark
density arrives. Every line carries the basis it used — a sheet that mixed
the two silently would be quoting two different claims under one column
header.
"""
import statistics
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.benchmark import account_key_for, compute_benchmarks
from app.db import bind_tenant
from app.models.invoice_line_item import InvoiceLineItem
from app.models.price_observation import PriceObservation
from app.models.tenant import Tenant

_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase4_analytics"]
_negotiation_thresholds = _thresholds["negotiation"]

TOP_N = _negotiation_thresholds["top_n"]
MIN_HISTORY_OBSERVATIONS = _negotiation_thresholds["min_history_observations"]
MIN_ANNUALIZATION_DAYS = _negotiation_thresholds["min_annualization_days"]
LOOKBACK_DAYS = _thresholds["benchmark"]["lookback_days"]

DAYS_PER_YEAR = Decimal(365)


class NegotiationBasis(str, Enum):
    peer = "peer"
    history = "history"
    auto = "auto"  # per SKU: peer when its cell clears suppression, else history


@dataclass
class NegotiationLine:
    canonical_sku_id: uuid.UUID
    basis: NegotiationBasis  # `peer` or `history` — never `auto`, which is a request-level choice
    current_price: Decimal
    current_price_line_item_id: uuid.UUID
    target_price: Decimal
    # Exactly one of these is set, matching `basis`: how many independent
    # businesses backed a peer target, or how many of the tenant's own prior
    # purchases backed a history one. Both are the "how do you know that"
    # answer a rep will ask for, so neither is optional on its own line.
    peer_account_count: int | None
    history_observation_count: int | None
    trailing_quantity: Decimal
    quantity_line_item_ids: list[uuid.UUID]
    recoverable_in_window: Decimal
    annualized_savings: Decimal


@dataclass
class NegotiationSheet:
    lines: list[NegotiationLine]
    # Days of price history the sheet is actually built from (<= LOOKBACK_DAYS).
    # Carried because every dollar figure on the sheet is a projection from it:
    # "$8,400/yr" from 90 days of invoices and from 11 days of invoices are not
    # the same claim, and the page says which one it is showing.
    window_days: int
    annualization_factor: Decimal
    total_annualized_savings: Decimal


def _annualization(observed_dates: list[date], as_of: date) -> tuple[int, Decimal]:
    """Projects a partial window to a year from the history actually present.

    Was a flat x4 ("a 90-day window is ~1 quarter"), which is only true once a
    customer has 90 days of invoices. Before that it understated every figure
    on the sheet — a tenant 30 days in had 30 days of quantity multiplied by 4,
    i.e. a third of their real annual exposure — on exactly the customers who
    have no peer benchmark either and so have the least to go on.

    The denominator is floored at MIN_ANNUALIZATION_DAYS so a tenant three
    deliveries in isn't extrapolated 100x off three data points. Erring short
    is the safe direction here: SPEC.md §1's failure mode is a number the rep
    debunks, and a conservative figure that holds up beats an aggressive one
    that doesn't.
    """
    span_days = (as_of - min(observed_dates)).days + 1
    factor = (DAYS_PER_YEAR / max(span_days, MIN_ANNUALIZATION_DAYS)).quantize(Decimal("0.0001"))
    return span_days, factor


def _history_target(prior_prices: list[Decimal]) -> Decimal | None:
    """The median of the tenant's own prior prices for a SKU: "this is what
    you normally paid, before the current price."

    Median rather than p25 (the statistic the peer basis uses) for two
    reasons. It is the same baseline app/analytics/price_creep.py computes, so
    a SKU's Insights creep alert and its negotiation line can't quote two
    different "before" prices for the same claim. And a quartile over a
    handful of a single tenant's own purchases is an interpolated number no
    invoice ever showed — priors of $2.00, $10.00, $10.00 (one spot buy,
    SPEC.md §4's `off_contract` phenomenon) interpolate to a $6.00 p25 the rep
    can debunk with "when did you ever pay that?", where the median answers
    $10.00, which is both true and the number worth arguing from. Across many
    tenants the peer basis has the sample size that makes p25 meaningful;
    within one tenant's history it does not.
    """
    if len(prior_prices) < MIN_HISTORY_OBSERVATIONS:
        return None
    # Quantized like every other money figure on the sheet (SPEC.md §11): an
    # even-length median averages the two middle prices, which lands on a 5th
    # decimal often enough that the page was rendering "$6.03395" in a column
    # of 4-decimal prices.
    return statistics.median(prior_prices).quantize(Decimal("0.0001"))  # median sorts internally


def build_negotiation_sheet(
    db: Session,
    tenant_id: uuid.UUID,
    as_of: date,
    basis: NegotiationBasis = NegotiationBasis.auto,
) -> NegotiationSheet:
    bind_tenant(db, tenant_id)
    tenant = db.get(Tenant, tenant_id)
    window_start = as_of - timedelta(days=LOOKBACK_DAYS)
    # Resolved once, not once per SKU: the asking tenant's business doesn't
    # change between rows of its own sheet.
    exclude_account_key = account_key_for(db, tenant_id)

    rows = db.execute(
        select(
            PriceObservation.canonical_sku_id,
            PriceObservation.observed_on,
            PriceObservation.unit_price_base,
            PriceObservation.invoice_line_item_id,
            InvoiceLineItem.normalized_qty_base,
        )
        .join(InvoiceLineItem, InvoiceLineItem.id == PriceObservation.invoice_line_item_id)
        .where(
            PriceObservation.tenant_id == tenant_id,
            PriceObservation.observed_on >= window_start,
            PriceObservation.observed_on <= as_of,
        )
        # Tertiary sort key: observed_on is a Date, so two deliveries of the
        # same SKU from two distributors on the same calendar date would
        # otherwise tie with no deterministic winner for "current price".
        .order_by(PriceObservation.canonical_sku_id, PriceObservation.observed_on.desc(), PriceObservation.id.desc())
    ).all()

    if not rows:
        return NegotiationSheet(
            lines=[], window_days=0, annualization_factor=Decimal(0), total_annualized_savings=Decimal("0.0000")
        )

    window_days, annualization_factor = _annualization([observed_on for _, observed_on, _, _, _ in rows], as_of)

    by_sku: dict[uuid.UUID, list[tuple[date, Decimal, uuid.UUID, Decimal]]] = {}
    for sku_id, observed_on, price, line_item_id, qty in rows:
        by_sku.setdefault(sku_id, []).append((observed_on, price, line_item_id, qty))

    benchmarks = (
        compute_benchmarks(
            db, list(by_sku), tenant.metro, as_of, volume_tier=None, exclude_account_key=exclude_account_key
        )
        if basis in (NegotiationBasis.peer, NegotiationBasis.auto)
        else {}
    )

    candidates: list[NegotiationLine] = []
    for sku_id, points in by_sku.items():
        current_price = points[0][1]  # points is sorted newest-first
        current_price_line_item_id = points[0][2]

        benchmark = benchmarks.get(sku_id)
        history_target = None
        if basis in (NegotiationBasis.history, NegotiationBasis.auto):
            # points[1:] is every prior purchase: the current price is excluded
            # from the target it's being compared against, for the same reason
            # the peer basis excludes the asker's own account — otherwise
            # today's overpayment quietly raises the bar it's measured against.
            prior_prices = [price for _, price, _, _ in points[1:]]
            history_target = _history_target(prior_prices)

        # A basis only counts as available for this SKU if it actually shows an
        # overpay. Deciding on "the peer cell exists" instead meant a SKU
        # priced under peer p25 was dropped outright and its own-history creep
        # never looked at — so a SKU could carry an open creep alert on the
        # Insights page and be silently missing from the default sheet. Peer
        # still wins whenever both have something to say: it is the stronger
        # argument, and a rep can't answer it with "that was our old price."
        if benchmark is not None and current_price > benchmark.p25:
            line_basis = NegotiationBasis.peer
            target_price = benchmark.p25
            peer_account_count: int | None = benchmark.distinct_account_count
            history_observation_count: int | None = None
        elif history_target is not None and current_price > history_target:
            line_basis = NegotiationBasis.history
            target_price = history_target
            peer_account_count = None
            history_observation_count = len(points) - 1
        else:
            continue  # nothing defensible to argue for this SKU yet

        trailing_qty = sum((qty for _, _, _, qty in points), Decimal(0))
        recoverable = ((current_price - target_price) * trailing_qty).quantize(Decimal("0.0001"))
        candidates.append(
            NegotiationLine(
                canonical_sku_id=sku_id,
                basis=line_basis,
                current_price=current_price,
                current_price_line_item_id=current_price_line_item_id,
                target_price=target_price,
                peer_account_count=peer_account_count,
                history_observation_count=history_observation_count,
                trailing_quantity=trailing_qty,
                quantity_line_item_ids=[lid for _, _, lid, _ in points],
                recoverable_in_window=recoverable,
                annualized_savings=(recoverable * annualization_factor).quantize(Decimal("0.0001")),
            )
        )

    candidates.sort(key=lambda line: line.recoverable_in_window, reverse=True)
    lines = candidates[:TOP_N]
    return NegotiationSheet(
        lines=lines,
        window_days=window_days,
        annualization_factor=annualization_factor,
        # Summed over the lines actually shown, in Decimal — SPEC.md §11's
        # money discipline applies to the total a rep will check against the
        # column above it just as much as to any per-line figure.
        total_annualized_savings=sum((line.annualized_savings for line in lines), Decimal(0)).quantize(
            Decimal("0.0001")
        ),
    )
