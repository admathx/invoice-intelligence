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

from app import email_design as design
from app import mail
from app.analytics.negotiation import NegotiationBasis, build_negotiation_sheet
from app.config import settings
from app.db import bind_tenant
from app.matching_queue import waiting_item_count
from app.models import (
    CanonicalSku,
    DigestSend,
    Distributor,
    Invoice,
    PriceAlert,
    Tenant,
    TenantMembership,
    User,
)
from app.models.enums import AlertStatus, InvoiceStatus

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=7)
# How long after the last price it's about an increase is still news to
# send: the weekly digest and the price-increase emails both go by it.
NEWS_FOR = timedelta(days=30)
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

    # New this week, and about prices still recent: a box of last year's
    # invoices added this week raises alerts that are history, not news
    # (as for the price-increase emails, app/alert_emails.py).
    news = (
        PriceAlert.tenant_id == tenant.id,
        PriceAlert.status == AlertStatus.open,
        PriceAlert.created_at >= since,
        PriceAlert.window_end >= (now - NEWS_FOR).date(),
    )
    new_alerts = select(PriceAlert).where(*news)
    week.new_increase_count = db.scalar(select(func.count()).select_from(new_alerts.subquery()))
    rows = db.execute(
        select(PriceAlert, CanonicalSku.name, CanonicalSku.base_uom, Distributor.name)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .outerjoin(Distributor, Distributor.id == PriceAlert.distributor_id)
        .where(*news)
        .order_by(PriceAlert.pct_change.desc())
        .limit(LISTED)
    ).all()
    week.new_increases = [
        PriceIncrease(
            design.product_from(name, distributor),
            a.baseline_price,
            a.current_price,
            a.pct_change,
            design.UNIT_LABEL.get(uom.value, uom.value),
        )
        for a, name, uom, distributor in rows
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
    week.pending_lines = waiting_item_count(db, tenant.id)

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


# One signed link per kind of email, so stopping the price alerts doesn't stop
# the digest. The digest's message is what it always was, so links in digests
# already sent keep working.
UNSUBSCRIBE_KINDS = ("digest", "alerts")


def unsubscribe_token(user_id: uuid.UUID, kind: str = "digest") -> str:
    return hmac.new(settings.signing_key, f"{kind}-unsubscribe:{user_id}".encode(), hashlib.sha256).hexdigest()


def valid_unsubscribe_token(user_id: uuid.UUID, token: str, kind: str = "digest") -> bool:
    return kind in UNSUBSCRIBE_KINDS and hmac.compare_digest(unsubscribe_token(user_id, kind), token)


def unsubscribe_url(user_id: uuid.UUID, kind: str = "digest") -> str:
    params = {"u": str(user_id), "t": unsubscribe_token(user_id, kind)}
    if kind != "digest":
        params["kind"] = kind
    return f"{settings.public_base_url}/api/digest/unsubscribe?{urlencode(params)}"


# --- The email ---------------------------------------------------------------


def _headline(weeks: list[LocationWeek]) -> str:
    increases = sum(w.new_increase_count for w in weeks)
    held = sum(w.held_count for w in weeks)
    parts = []
    if increases:
        parts.append(f"{increases} price increase{'s' if increases != 1 else ''}")
    if held:
        parts.append(f"{held} invoice{'s' if held != 1 else ''} to look at")
    where = weeks[0].name if len(weeks) == 1 else f"{len(weeks)} locations"
    return f"Your week at {where}: " + (", ".join(parts) if parts else "all quiet")


def _sections_text(week: LocationWeek) -> list[str]:
    out = [week.name, "=" * len(week.name)]
    if week.new_increase_count:
        out.append(f"New price increases ({week.new_increase_count}):")
        out += [design.increase_line(p) for p in week.new_increases]
        if week.new_increase_count > len(week.new_increases):
            out.append(f"  ...and {week.new_increase_count - len(week.new_increases)} more")
        out.append(f"  {design.dashboard_link('/insights', week.tenant_id)}")
    if week.held_count:
        out.append(f"Invoices that need a look ({week.held_count}): their prices aren't used until they're fixed")
        for h in week.held:
            out.append(f"  - {h.label}: {design.dashboard_link(f'/invoices/{h.invoice_id}', week.tenant_id)}")
    if week.pending_lines:
        out.append(f"Match items: {week.pending_lines} item{'s' if week.pending_lines != 1 else ''} to match")
        out.append(f"  {design.dashboard_link('/review', week.tenant_id)}")
    if week.received_count:
        out.append(f"Received this week: {week.received_count} invoice{'s' if week.received_count != 1 else ''}, {design.money(week.received_total)}")
    if week.savings:
        out.append("Your biggest savings:")
        for s in week.savings:
            out.append(f"  - {s.sku}: about {design.money(s.annualized)} a year")
        out.append(f"  {design.dashboard_link('/negotiation', week.tenant_id)}")
    return out


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
        return design.dashboard_link(path, week.tenant_id)

    if week.new_increase_count:
        more = week.new_increase_count - len(week.new_increases)
        parts.append(
            f"<p style='margin:0 0 6px'><strong style='color:{design.RED_TEXT}'>{week.new_increase_count} new price "
            f"increase{'s' if week.new_increase_count != 1 else ''}</strong></p>"
            f"{design.increase_table(week.new_increases, more)}"
            f"<p style='margin:10px 0 0'>{design.button(link('/insights'), 'See price alerts')}</p>"
        )
    if week.held_count:
        items = "".join(
            f"<li style='margin:2px 0;overflow-wrap:anywhere'><a href='{e(link(f'/invoices/{h.invoice_id}'))}' style='color:{design.GREEN_TEXT};font-weight:600'>"
            f"{e(h.label)}</a></li>"
            for h in week.held
        )
        parts.append(
            f"<div style='margin:16px 0 0;padding:10px 12px;background:{design.AMBER_TINT};border-radius:6px;color:{design.AMBER_TEXT}'>"
            f"<strong>{week.held_count} invoice{'s' if week.held_count != 1 else ''} need{'s' if week.held_count == 1 else ''} a look</strong> "
            "&middot; their prices aren&rsquo;t used until they&rsquo;re fixed"
            f"<ul style='margin:6px 0 0;padding-left:20px'>{items}</ul></div>"
        )
    if week.pending_lines:
        lines = f"{week.pending_lines} item{'s' if week.pending_lines != 1 else ''}"
        parts.append(
            f"<p style='margin:16px 0 0'><strong>Match items:</strong> {design.pill(lines + ' to match', design.AMBER_TEXT, design.AMBER_TINT)} "
            f"&nbsp;{design.button(link('/review'), 'Match them')}</p>"
        )
    if week.received_count:
        invoices = f"{week.received_count} invoice{'s' if week.received_count != 1 else ''}"
        parts.append(
            f"<p style='margin:16px 0 0'><strong>Received this week:</strong> {invoices}, "
            f"<strong>{design.money(week.received_total)}</strong></p>"
        )
    if week.savings:
        rows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0'>{e(s.sku)}</td>"
            f"<td style='padding:3px 0;text-align:right;color:{design.GREEN_TEXT};font-weight:700'>{design.money(s.annualized)}/yr</td></tr>"
            for s in week.savings
        )
        total = sum((s.annualized for s in week.savings), Decimal("0"))
        parts.append(
            f"<div style='margin:16px 0 0;padding:12px 14px;background:{design.GREEN_TINT};border:1px solid #bbf7d0;border-radius:6px'>"
            f"<p style='margin:0 0 6px;color:{design.GREEN_TEXT}'><strong>Your biggest savings</strong> "
            f"&middot; about <strong>{design.money(total)}</strong> a year</p>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;border-collapse:collapse'>{rows}</table>"
            f"<p style='margin:10px 0 0'>{design.button(link('/negotiation'), 'See savings')}</p></div>"
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
    html_doc = design.document(
        tag="Your week",
        body=(
            f"<p style='margin:0 0 4px'>Hi {html.escape(user.name)},</p>"
            f"<p style='margin:0;color:{design.MUTED}'>Here&rsquo;s what happened this week, and what&rsquo;s waiting for you.</p>"
            f"{body}"
        ),
        footer=(
            "You get this weekly because you have access to these locations. "
            f"<a href='{html.escape(unsubscribe)}' style='color:{design.MUTED}'>Stop these emails</a> "
            f"or turn them back on from <a href='{html.escape(account)}' style='color:{design.MUTED}'>your account</a>."
        ),
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
