"""How the emails look: the weekly digest, price-increase alerts and
password resets share one frame, palette and button.

The app's palette (frontend tailwind.config.ts `brand`) is inlined: email
clients ignore stylesheets. Increases red, savings green, like the dashboard.
"""
import html
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Protocol

from app.config import settings

GREEN_FILL = "#4ade80"  # brand-400: buttons, header band
GREEN_INK = "#052e16"  # brand-950: text on the fill
GREEN_TEXT = "#15803d"  # brand-700: savings, good news
GREEN_TINT = "#f1fdf4"  # brand-50
RED_TEXT = "#dc2626"
RED_TINT = "#fee2e2"
AMBER_TEXT = "#92400e"
AMBER_TINT = "#fef3c7"
MUTED = "#6b7280"


def button(href: str, label: str) -> str:
    """A button that survives email clients: a padded, filled link (clients
    that drop padding still show a green link)."""
    return (
        f"<a href='{html.escape(href)}' style='display:inline-block;background:{GREEN_FILL};color:{GREEN_INK};"
        "font-weight:700;font-size:13px;text-decoration:none;padding:8px 14px;border-radius:6px;"
        f"border:1px solid #22c55e'>{html.escape(label)}</a>"
    )


def pill(text: str, fg: str, bg: str) -> str:
    return (
        f"<span style='display:inline-block;white-space:nowrap;background:{bg};color:{fg};font-weight:700;font-size:12px;"
        f"padding:2px 8px;border-radius:999px'>{html.escape(text)}</span>"
    )


def document(*, tag: str, body: str, footer: str) -> str:
    """The frame every email shares: the app's green header band with its
    mark, `tag` on the right of it, then `body`, then a small-print `footer`.
    `body` and `footer` are HTML, already escaped by the caller."""
    return (
        # The viewport line: without it a phone's mail app lays the email out
        # at desktop width and shrinks the whole thing.
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
        "<body style='margin:0;padding:24px 12px;background:#f3f4f6'>"
        "<div style='max-width:600px;margin:0 auto;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "font-size:14px;color:#111827;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb'>"
        # The app's header, in miniature: the green band and the mark.
        f"<div style='background:{GREEN_FILL};padding:14px 24px;color:{GREEN_INK}'>"
        "<span style='display:inline-block;background:#ffffff;color:#15803d;font-weight:800;font-size:12px;"
        "padding:3px 7px;border-radius:5px;margin-right:8px'>II</span>"
        "<strong style='font-size:15px'>Invoice Intelligence</strong>"
        f"<span style='float:right;font-size:13px;font-weight:600;padding-top:3px'>{html.escape(tag)}</span></div>"
        f"<div style='padding:20px 24px 24px'>{body}"
        "<hr style='border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px'>"
        f"<p style='font-size:12px;color:{MUTED};margin:0'>{footer}</p>"
        "</div></div></body></html>"
    )


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


# Base units as people write them. Prices are per base unit ("$0.54" for
# cilantro is per pound); without the unit the number reads as per case.
UNIT_LABEL = {"lb": "lb", "oz": "oz", "gal": "gal", "fl_oz": "fl oz", "each": "each", "dozen": "dozen"}


def unit_price(value: Decimal) -> str:
    """A price per unit, where fractions of a cent matter on small items
    (as the dashboard shows them, frontend/src/lib/format.ts): cents from
    $1 up, up to four places below it and six below a cent, zeros dropped
    past the cent. To the cent, a napkin going from $0.0125 to $0.0131 read
    "$0.01 -> $0.01"."""
    places = 2 if abs(value) >= 1 else 4 if abs(value) >= Decimal("0.01") else 6
    whole, _, fraction = f"{value:,.{places}f}".partition(".")
    return f"${whole}.{fraction.rstrip('0').ljust(2, '0')}"


def price_per(value: Decimal, unit: str) -> str:
    return f"{unit_price(value)}/{unit}" if unit else unit_price(value)


def dashboard_link(path: str, location: uuid.UUID) -> str:
    """A link into the dashboard at one location. ?location= makes the
    dashboard switch to it on arrival (frontend middleware), so a link from
    one location's section of an email never opens in another's."""
    joiner = "&" if "?" in path else "?"
    return f"{settings.public_base_url}{path}{joiner}location={location}"


@dataclass(frozen=True)
class Elsewhere:
    """Where an increase's product costs less, and what to do about it: the
    cheapest of the alert's alternatives, ready to show (app/digest.py
    elsewhere_for; the reasoning is app/analytics/switching.py)."""

    distributor: str
    website: str | None
    price: Decimal
    less: Decimal  # than the price that was flagged, 0..1
    # "about $1,112", or None when there's no yearly figure to give.
    saves: str | None
    # What to do, in a few words, and a page that helps do it.
    action: str
    action_link: str | None = None


class PriceIncreaseRow(Protocol):
    sku: str
    before: Decimal
    now: Decimal
    pct_change: Decimal
    unit: str
    elsewhere: Elsewhere | None


def product_from(product: str, distributor: str | None) -> str:
    """How an increase names what went up: the product, and whose price."""
    return f"{product} from {distributor}" if distributor else product


def increase_line(p: PriceIncreaseRow) -> str:
    """One increase in a plain-text email, and under it where it costs less."""
    line = f"  - {p.sku}: {price_per(p.before, p.unit)} -> {price_per(p.now, p.unit)} (+{p.pct_change:.1%})"
    there = p.elsewhere
    if there is None:
        return line
    site = f" ({there.website})" if there.website else ""
    saves = f", saves {there.saves} a year" if there.saves else ""
    return (
        f"{line}\n      Cheaper at {there.distributor}{site}: {price_per(there.price, p.unit)}{saves}."
        f"\n      Next step: {there.action}."
    )


def increase_table(increases: Iterable[PriceIncreaseRow], more: int = 0) -> str:
    """Price increases as the emails show them: product, before → now, and
    a red pill. Shared by the digest and the price-increase email, so a fix
    for one screen size is a fix for both."""
    e = html.escape
    rows = "".join(
        "<tr>"
        f"<td style='padding:4px 12px 4px 0'>{e(p.sku)}</td>"
        # Each price stays whole; the pair may wrap at the arrow, so a
        # narrow screen doesn't crush the product name instead.
        f"<td style='padding:4px 12px 4px 0;color:{MUTED}'><span style='white-space:nowrap'>{e(price_per(p.before, p.unit))} &rarr;</span> "
        f"<strong style='color:#111827;white-space:nowrap'>{e(price_per(p.now, p.unit))}</strong></td>"
        f"<td style='padding:4px 0;text-align:right'>{pill(f'▲ +{p.pct_change:.1%}', RED_TEXT, RED_TINT)}</td>"
        "</tr>"
        for p in increases
    )
    more_html = f"<p style='margin:4px 0 0;color:{MUTED}'>&hellip;and {more} more</p>" if more > 0 else ""
    return (
        f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;border-collapse:collapse'>{rows}</table>"
        f"{more_html}"
    )


def elsewhere_table(increases: Iterable[PriceIncreaseRow]) -> str:
    """The increases whose product costs less at another distributor: where,
    for how much, what that saves in a year, and what to do. A table, since
    it's read across; nothing at all when no product has anywhere cheaper.
    The distributor links to its own site and the next step to the page
    that helps take it."""
    e = html.escape
    link = f"color:{GREEN_TEXT};font-weight:600"
    # Narrow gaps and nothing forced onto one line but a single figure: on a
    # phone four columns share about 280px, and a no-wrap cell pushed the
    # next step off the edge of the email.
    cell = "padding:6px 8px 6px 0;vertical-align:top;border-top:1px solid #e5e7eb"
    whole = "white-space:nowrap"
    rows = ""
    for p in increases:
        there = p.elsewhere
        if there is None:
            continue
        where = f"<a href='{e(there.website)}' style='{link}'>{e(there.distributor)}</a>" if there.website else e(there.distributor)
        action = (
            f"<a href='{e(there.action_link)}' style='{link}'>{e(there.action)}</a>"
            if there.action_link
            else f"<strong>{e(there.action)}</strong>"
        )
        rows += (
            f"<tr><td style='{cell}'>{e(p.sku)}</td>"
            f"<td style='{cell}'>{where}<br><span style='color:{MUTED};font-size:12px'>"
            f"<span style='{whole}'>{e(price_per(there.price, p.unit))},</span> "
            f"<span style='{whole}'>{there.less:.0%} less</span></span></td>"
            f"<td style='{cell}'>{e(there.saves) if there.saves else '&mdash;'}</td>"
            f"<td style='{cell};padding-right:0'>{action}</td></tr>"
        )
    if not rows:
        return ""
    head = "padding:0 8px 4px 0;text-align:left;vertical-align:bottom;font-size:12px;font-weight:600"
    return (
        f"<p style='margin:16px 0 6px'><strong style='color:{GREEN_TEXT}'>Where they cost less</strong></p>"
        "<table cellpadding='0' cellspacing='0' style='font-size:13px;border-collapse:collapse;width:100%'>"
        f"<tr style='color:{MUTED}'><th style='{head}'>Product</th><th style='{head}'>Cheaper at</th>"
        f"<th style='{head}'>Saves a year</th><th style='{head};padding-right:0'>Next step</th></tr>"
        f"{rows}</table>"
    )
