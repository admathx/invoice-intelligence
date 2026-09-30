"""How the emails look: the weekly digest, price-increase alerts and
password resets share one frame, palette and button.

The app's palette (frontend tailwind.config.ts `brand`) is inlined: email
clients ignore stylesheets. Increases red, savings green, like the dashboard.
"""
import html
import uuid
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


def price_per(value: Decimal, unit: str) -> str:
    return f"{money(value)}/{unit}" if unit else money(value)


def dashboard_link(path: str, location: uuid.UUID) -> str:
    """A link into the dashboard at one location. ?location= makes the
    dashboard switch to it on arrival (frontend middleware), so a link from
    one location's section of an email never opens in another's."""
    joiner = "&" if "?" in path else "?"
    return f"{settings.public_base_url}{path}{joiner}location={location}"


class PriceIncreaseRow(Protocol):
    sku: str
    before: Decimal
    now: Decimal
    pct_change: Decimal
    unit: str


def product_from(product: str, distributor: str | None) -> str:
    """How an increase names what went up: the product, and whose price."""
    return f"{product} from {distributor}" if distributor else product


def increase_line(p: PriceIncreaseRow) -> str:
    """One increase in a plain-text email."""
    return f"  - {p.sku}: {price_per(p.before, p.unit)} -> {price_per(p.now, p.unit)} (+{p.pct_change:.1%})"


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
