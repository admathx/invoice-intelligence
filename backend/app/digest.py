"""The weekly digest: one email per person, a section per location they belong to.

Price-creep alerts and invoices that need a look only help if someone opens
the dashboard. This brings the week to them: new price increases, invoices
that couldn't be read or don't add up, lines waiting in the review queue,
what arrived, and the biggest savings on the negotiation sheet. A location
with a quiet week is left out; a person whose every location was quiet gets
nothing that week.

Sending is idempotent: each (person, week) is claimed in digest_sends before
the email goes out, under a unique constraint, so a scheduler restart, a
second scheduler, or a re-run can never send the same week twice. A send
that fails releases its claim, so the next run tries again.
"""
import hashlib
import hmac
import html
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from urllib.parse import urlencode

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import mail
from app.analytics.negotiation import NegotiationBasis, build_negotiation_sheet
from app.config import settings
from app.db import bind_tenant
from app.models import (
    CanonicalSku,
    DigestSend,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceAlert,
    Tenant,
    TenantMembership,
    User,
)
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import AlertStatus, InvoiceStatus, ReviewStatus

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
LISTED = 5  # items of each kind shown before "and N more"
SAVINGS_LISTED = 3


@dataclass
class PriceIncrease:
    sku: str
    before: Decimal
    now: Decimal
    pct_change: Decimal
    # Prices are per base unit ("$0.54" for cilantro is per pound); without
    # it the number reads as per case.
    unit: str = ""


@dataclass
class HeldInvoice:
    invoice_id: uuid.UUID
    label: str
    status: str


@dataclass
class Saving:
    sku: str
    annualized: Decimal


@dataclass
class LocationWeek:
    tenant_id: uuid.UUID
    name: str
    new_increases: list[PriceIncrease] = field(default_factory=list)
    new_increase_count: int = 0
    open_alert_count: int = 0
    held: list[HeldInvoice] = field(default_factory=list)
    held_count: int = 0
    pending_lines: int = 0
    received_count: int = 0
    received_total: Decimal = Decimal("0")
    savings: list[Saving] = field(default_factory=list)

    @property
    def has_news(self) -> bool:
        """Something happened or needs doing. Savings alone don't count: they
        change slowly, and a digest of only those every week is noise."""
        return bool(self.new_increase_count or self.held_count or self.pending_lines or self.received_count)


def week_of(now: datetime) -> date:
    """The Monday of the week `now` falls in: the key a send is recorded under."""
    day = now.astimezone(timezone.utc).date()
    return day - timedelta(days=day.weekday())


def location_week(db: Session, tenant: Tenant, now: datetime) -> LocationWeek:
    since = now - WINDOW
    bind_tenant(db, tenant.id)
    week = LocationWeek(tenant_id=tenant.id, name=tenant.name)

    new_alerts = select(PriceAlert).where(
        PriceAlert.tenant_id == tenant.id, PriceAlert.status == AlertStatus.open, PriceAlert.created_at >= since
    )
    week.new_increase_count = db.scalar(select(func.count()).select_from(new_alerts.subquery()))
    rows = db.execute(
        select(PriceAlert, CanonicalSku.name, CanonicalSku.base_uom)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .where(PriceAlert.tenant_id == tenant.id, PriceAlert.status == AlertStatus.open, PriceAlert.created_at >= since)
        .order_by(PriceAlert.pct_change.desc())
        .limit(LISTED)
    ).all()
    week.new_increases = [
        PriceIncrease(name, a.baseline_price, a.current_price, a.pct_change, _UNIT.get(uom.value, uom.value))
        for a, name, uom in rows
    ]
    week.open_alert_count = db.scalar(
        select(func.count(PriceAlert.id)).where(PriceAlert.tenant_id == tenant.id, PriceAlert.status == AlertStatus.open)
    )

    held_statuses = [InvoiceStatus.needs_review, InvoiceStatus.failed]
    week.held_count = db.scalar(
        select(func.count(Invoice.id)).where(Invoice.tenant_id == tenant.id, Invoice.status.in_(held_statuses))
    )
    week.held = [
        HeldInvoice(inv.id, inv.invoice_number or f"received {inv.created_at:%b %-d}", inv.status.value)
        for inv in db.scalars(
            select(Invoice)
            .where(Invoice.tenant_id == tenant.id, Invoice.status.in_(held_statuses))
            .order_by(Invoice.created_at.desc())
            .limit(LISTED)
        )
    ]

    # Counted exactly as the review queue lists them (app/api/review.py):
    # lines on invoices with no recognized distributor aren't in it yet.
    week.pending_lines = db.scalar(
        select(func.count(InvoiceLineItem.id))
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .join(Distributor, Distributor.id == Invoice.distributor_id)
        .where(
            InvoiceLineItem.tenant_id == tenant.id,
            InvoiceLineItem.review_status == ReviewStatus.pending,
            Distributor.slug != UNRECOGNIZED_SLUG,
        )
    )

    week.received_count, total = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.tenant_id == tenant.id,
            Invoice.created_at >= since,
            Invoice.status.in_([InvoiceStatus.extracted, InvoiceStatus.confirmed]),
        )
    ).one()
    week.received_total = Decimal(total)

    sheet = build_negotiation_sheet(db, tenant.id, now.date(), NegotiationBasis.auto)
    top = [line for line in sheet.lines if line.annualized_savings > 0][:SAVINGS_LISTED]
    names = dict(
        db.execute(
            select(CanonicalSku.id, CanonicalSku.name).where(CanonicalSku.id.in_([line.canonical_sku_id for line in top]))
        ).all()
    )
    week.savings = [Saving(names.get(line.canonical_sku_id, "(unknown SKU)"), line.annualized_savings) for line in top]
    return week


def recipients(db: Session) -> list[tuple[User, list[Tenant]]]:
    """Active people who want the digest, with the locations they belong to.
    Operators count only for locations they're members of: otherwise they'd
    get every location in the system every week."""
    people: dict[uuid.UUID, tuple[User, list[Tenant]]] = {}
    for user, tenant in db.execute(
        select(User, Tenant)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(User.is_active.is_(True), User.digest_enabled.is_(True))
        .order_by(User.email, Tenant.name)
    ):
        people.setdefault(user.id, (user, []))[1].append(tenant)
    return list(people.values())


# --- Unsubscribing -----------------------------------------------------------


def unsubscribe_token(user_id: uuid.UUID) -> str:
    return hmac.new(settings.signing_key, f"digest-unsubscribe:{user_id}".encode(), hashlib.sha256).hexdigest()


def valid_unsubscribe_token(user_id: uuid.UUID, token: str) -> bool:
    return hmac.compare_digest(unsubscribe_token(user_id), token)


def unsubscribe_url(user_id: uuid.UUID) -> str:
    query = urlencode({"u": str(user_id), "t": unsubscribe_token(user_id)})
    return f"{settings.public_base_url}/api/digest/unsubscribe?{query}"


# --- The email ---------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


_UNIT = {"lb": "lb", "oz": "oz", "gal": "gal", "fl_oz": "fl oz", "each": "each", "dozen": "dozen"}


def _per(value: Decimal, unit: str) -> str:
    return f"{_money(value)}/{unit}" if unit else _money(value)


def _link(path: str, location: uuid.UUID) -> str:
    # ?location= makes the dashboard switch to that location on arrival
    # (frontend middleware), so a link from one location's section never
    # opens in another's.
    joiner = "&" if "?" in path else "?"
    return f"{settings.public_base_url}{path}{joiner}location={location}"


def _headline(weeks: list[LocationWeek]) -> str:
    increases = sum(w.new_increase_count for w in weeks)
    held = sum(w.held_count for w in weeks)
    parts = []
    if increases:
        parts.append(f"{increases} price increase{'s' if increases != 1 else ''}")
    if held:
        parts.append(f"{held} invoice{'s' if held != 1 else ''} to check")
    where = weeks[0].name if len(weeks) == 1 else f"{len(weeks)} locations"
    return f"Your week at {where}: " + (", ".join(parts) if parts else "all quiet")


def _sections_text(week: LocationWeek) -> list[str]:
    out = [week.name, "=" * len(week.name)]
    if week.new_increase_count:
        out.append(f"New price increases ({week.new_increase_count}):")
        for p in week.new_increases:
            out.append(f"  - {p.sku}: {_per(p.before, p.unit)} -> {_per(p.now, p.unit)} (+{p.pct_change:.1%})")
        if week.new_increase_count > len(week.new_increases):
            out.append(f"  ...and {week.new_increase_count - len(week.new_increases)} more")
        out.append(f"  {_link('/insights', week.tenant_id)}")
    if week.held_count:
        out.append(f"Invoices to check ({week.held_count}): held out of your numbers until someone does")
        for h in week.held:
            out.append(f"  - {h.label}: {_link(f'/invoices/{h.invoice_id}', week.tenant_id)}")
    if week.pending_lines:
        out.append(f"Review queue: {week.pending_lines} line{'s' if week.pending_lines != 1 else ''} to match")
        out.append(f"  {_link('/review', week.tenant_id)}")
    if week.received_count:
        out.append(f"Received this week: {week.received_count} invoice{'s' if week.received_count != 1 else ''}, {_money(week.received_total)}")
    if week.savings:
        out.append("Biggest savings on your negotiation sheet:")
        for s in week.savings:
            out.append(f"  - {s.sku}: about {_money(s.annualized)} a year")
        out.append(f"  {_link('/negotiation', week.tenant_id)}")
    return out


# The app's palette (frontend tailwind.config.ts `brand`), inlined: email
# clients ignore stylesheets. Increases red, savings green, like the dashboard.
_GREEN_FILL = "#4ade80"  # brand-400: buttons, header band
_GREEN_INK = "#052e16"  # brand-950: text on the fill
_GREEN_TEXT = "#15803d"  # brand-700: savings, good news
_GREEN_TINT = "#f1fdf4"  # brand-50
_RED_TEXT = "#dc2626"
_RED_TINT = "#fee2e2"
_AMBER_TEXT = "#92400e"
_AMBER_TINT = "#fef3c7"
_MUTED = "#6b7280"


def _button(href: str, label: str) -> str:
    """A button that survives email clients: a padded, filled link (clients
    that drop padding still show a green link)."""
    return (
        f"<a href='{html.escape(href)}' style='display:inline-block;background:{_GREEN_FILL};color:{_GREEN_INK};"
        "font-weight:700;font-size:13px;text-decoration:none;padding:8px 14px;border-radius:6px;"
        f"border:1px solid #22c55e'>{html.escape(label)}</a>"
    )


def _pill(text: str, fg: str, bg: str) -> str:
    return (
        f"<span style='display:inline-block;white-space:nowrap;background:{bg};color:{fg};font-weight:700;font-size:12px;"
        f"padding:2px 8px;border-radius:999px'>{html.escape(text)}</span>"
    )


def _sections_html(week: LocationWeek) -> str:
    e = html.escape
    # A block, not a 100%-wide table: a table's border adds to its width, and
    # on a phone the box ran past the edge of the email.
    parts = [
        "<div style='margin:20px 0 0;border:1px solid #e5e7eb;border-left:4px solid #86efac;border-radius:8px;"
        "padding:16px 18px'>",
        f"<h2 style='font-size:17px;margin:0 0 10px'>{e(week.name)}</h2>",
    ]

    def link(path: str) -> str:
        return _link(path, week.tenant_id)

    if week.new_increase_count:
        rows = "".join(
            "<tr>"
            f"<td style='padding:4px 12px 4px 0'>{e(p.sku)}</td>"
            # Each price stays whole; the pair may wrap at the arrow, so a
            # narrow screen doesn't crush the product name instead.
            f"<td style='padding:4px 12px 4px 0;color:{_MUTED}'><span style='white-space:nowrap'>{e(_per(p.before, p.unit))} &rarr;</span> "
            f"<strong style='color:#111827;white-space:nowrap'>{e(_per(p.now, p.unit))}</strong></td>"
            f"<td style='padding:4px 0;text-align:right'>{_pill(f'▲ +{p.pct_change:.1%}', _RED_TEXT, _RED_TINT)}</td>"
            "</tr>"
            for p in week.new_increases
        )
        more = week.new_increase_count - len(week.new_increases)
        more_html = f"<p style='margin:4px 0 0;color:{_MUTED}'>&hellip;and {more} more</p>" if more > 0 else ""
        parts.append(
            f"<p style='margin:0 0 6px'><strong style='color:{_RED_TEXT}'>{week.new_increase_count} new price "
            f"increase{'s' if week.new_increase_count != 1 else ''}</strong></p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;border-collapse:collapse'>{rows}</table>"
            f"{more_html}<p style='margin:10px 0 0'>{_button(link('/insights'), 'See them on Insights')}</p>"
        )
    if week.held_count:
        items = "".join(
            f"<li style='margin:2px 0;overflow-wrap:anywhere'><a href='{e(link(f'/invoices/{h.invoice_id}'))}' style='color:{_GREEN_TEXT};font-weight:600'>"
            f"{e(h.label)}</a></li>"
            for h in week.held
        )
        parts.append(
            f"<div style='margin:16px 0 0;padding:10px 12px;background:{_AMBER_TINT};border-radius:6px;color:{_AMBER_TEXT}'>"
            f"<strong>{week.held_count} invoice{'s' if week.held_count != 1 else ''} to check</strong> "
            "&middot; held out of your numbers until someone does"
            f"<ul style='margin:6px 0 0;padding-left:20px'>{items}</ul></div>"
        )
    if week.pending_lines:
        lines = f"{week.pending_lines} line{'s' if week.pending_lines != 1 else ''}"
        parts.append(
            f"<p style='margin:16px 0 0'><strong>Review queue:</strong> {_pill(lines + ' to match', _AMBER_TEXT, _AMBER_TINT)} "
            f"&nbsp;{_button(link('/review'), 'Open the queue')}</p>"
        )
    if week.received_count:
        invoices = f"{week.received_count} invoice{'s' if week.received_count != 1 else ''}"
        parts.append(
            f"<p style='margin:16px 0 0'><strong>Received this week:</strong> {invoices}, "
            f"<strong>{_money(week.received_total)}</strong></p>"
        )
    if week.savings:
        rows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0'>{e(s.sku)}</td>"
            f"<td style='padding:3px 0;text-align:right;color:{_GREEN_TEXT};font-weight:700'>{_money(s.annualized)}/yr</td></tr>"
            for s in week.savings
        )
        total = sum((s.annualized for s in week.savings), Decimal("0"))
        parts.append(
            f"<div style='margin:16px 0 0;padding:12px 14px;background:{_GREEN_TINT};border:1px solid #bbf7d0;border-radius:6px'>"
            f"<p style='margin:0 0 6px;color:{_GREEN_TEXT}'><strong>Biggest savings on your negotiation sheet</strong> "
            f"&middot; about <strong>{_money(total)}</strong> a year</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;border-collapse:collapse'>{rows}</table>"
            f"<p style='margin:10px 0 0'>{_button(link('/negotiation'), 'Open the sheet')}</p></div>"
        )
    parts.append("</div>")
    return "".join(parts)


def compose(user: User, weeks: list[LocationWeek]) -> EmailMessage | None:
    """The digest for one person, or None when none of their locations had
    anything this week."""
    weeks = [w for w in weeks if w.has_news]
    if not weeks:
        return None
    subject = _headline(weeks)
    unsubscribe = unsubscribe_url(user.id)
    account = f"{settings.public_base_url}/account/password"

    text_lines = [f"Hi {user.name},", "", "Here's your week.", ""]
    for week in weeks:
        text_lines += _sections_text(week) + [""]
    text_lines += [
        "--",
        f"You get this weekly because you have access to {', '.join(w.name for w in weeks)}.",
        f"Stop these emails: {unsubscribe}",
    ]

    body = "".join(_sections_html(w) for w in weeks)
    html_doc = (
        "<!doctype html><html><body style='margin:0;padding:24px 12px;background:#f3f4f6'>"
        "<div style='max-width:600px;margin:0 auto;font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        "font-size:14px;color:#111827;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e5e7eb'>"
        # The app's header, in miniature: the green band and the mark.
        f"<div style='background:{_GREEN_FILL};padding:14px 24px;color:{_GREEN_INK}'>"
        "<span style='display:inline-block;background:#ffffff;color:#15803d;font-weight:800;font-size:12px;"
        "padding:3px 7px;border-radius:5px;margin-right:8px'>II</span>"
        "<strong style='font-size:15px'>Invoice Intelligence</strong>"
        "<span style='float:right;font-size:13px;font-weight:600;padding-top:3px'>Your week</span></div>"
        "<div style='padding:20px 24px 24px'>"
        f"<p style='margin:0 0 4px'>Hi {html.escape(user.name)},</p>"
        f"<p style='margin:0;color:{_MUTED}'>Here&rsquo;s what happened this week, and what&rsquo;s waiting for you.</p>"
        f"{body}"
        "<hr style='border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px'>"
        f"<p style='font-size:12px;color:{_MUTED};margin:0'>You get this weekly because you have access to these locations. "
        f"<a href='{html.escape(unsubscribe)}' style='color:{_MUTED}'>Stop these emails</a> "
        f"or turn them back on from <a href='{html.escape(account)}' style='color:{_MUTED}'>your account</a>.</p>"
        "</div></div></body></html>"
    )
    return mail.build_message(
        to=user.email,
        subject=subject,
        text="\n".join(text_lines),
        html=html_doc,
        headers={
            # RFC 8058: mail clients show their own unsubscribe button, and a
            # one-click POST to this URL works without opening anything.
            "List-Unsubscribe": f"<{unsubscribe}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )


# --- Sending ----------------------------------------------------------------


@dataclass
class DigestRun:
    week_of: date
    sent: int = 0
    quiet: int = 0
    already_sent: int = 0
    failed: list[str] = field(default_factory=list)


def _claim(db: Session, user: User, week: date, location_count: int) -> bool:
    """Record this person's digest for this week before sending it. False if
    another run already did: the unique constraint decides, not a read."""
    try:
        with db.begin_nested():
            db.add(DigestSend(user_id=user.id, week_of=week, location_count=location_count))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def send_due_digests(db: Session, now: datetime | None = None, *, only: list[uuid.UUID] | None = None) -> DigestRun:
    """Send this week's digest to everyone who hasn't had it. `only` limits it
    to those user ids."""
    now = now or datetime.now(timezone.utc)
    run = DigestRun(week_of=week_of(now))
    for user, tenants in recipients(db):
        if only is not None and user.id not in only:
            continue
        if db.scalar(select(DigestSend.id).where(DigestSend.user_id == user.id, DigestSend.week_of == run.week_of)):
            run.already_sent += 1
            continue
        weeks = [location_week(db, tenant, now) for tenant in tenants]
        message = compose(user, weeks)
        if not _claim(db, user, run.week_of, sum(w.has_news for w in weeks)):
            run.already_sent += 1
            continue
        if message is None:
            run.quiet += 1  # recorded all the same, so the week isn't recomputed on every run
            continue
        try:
            mail.send(message)
            run.sent += 1
        except mail.MailError as exc:
            # Release the claim so the next run tries again.
            db.execute(delete(DigestSend).where(DigestSend.user_id == user.id, DigestSend.week_of == run.week_of))
            db.commit()
            run.failed.append(f"{user.email}: {exc}")
            logger.warning("digest to %s failed: %s", user.email, exc)
    return run


def is_due(now: datetime) -> bool:
    """Past this week's send time (the scheduler sends anything unsent once it is)."""
    start = datetime.combine(week_of(now), datetime.min.time(), tzinfo=timezone.utc)
    due = start + timedelta(days=settings.digest_weekday, hours=settings.digest_hour_utc)
    return now >= due
