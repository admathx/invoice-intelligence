"""Price-increase emails: the same day a big increase shows up, not Monday.

A price alert opens when a location's recent prices for a product have
climbed clear of its baseline (app/analytics/price_creep.py), which happens
as soon as the invoice that shows it is read or reviewed. The weekly digest
reports every new one; this sends the big ones (settings.
alert_email_min_pct_change and up) soon after, to everyone at that
location who wants them, so a distributor's quiet 15% on a staple can be
queried before the next order rather than a week of orders later.

Two things hold an alert back. A box of old invoices added in whatever
order they come out builds each product's history in pieces, and a piece
can look like an increase that the rest of the box takes away again: on a
realistic set a lime alert opened and closed twice that way. So a
location's alerts wait until its invoices have stopped arriving (none added
in the last SETTLE, none still being read), and only what is still open
then is sent. And an increase is only news while the prices it is about are
recent (NEWS_FOR, long enough for a month's invoices added at month end):
what a distributor charged last spring is history, shown on the Price
alerts page, not something to email about today.

One email per person per run, covering every new alert across their
locations, so an invoice that opens five alerts sends one message, not five.

Idempotent per (alert, person): each pair is claimed in alert_email_sends
before the email goes out, under a unique constraint, and only pairs this run
claimed are sent. A failed send releases its claims, so the next run retries.

A product already emailed to someone at a location isn't emailed to them
again for REPEAT_QUIET, even as a new alert: a price that wobbles around the
threshold closes and reopens its alert, and each reopening isn't news.

Only alerts opened in the last LOOKBACK are considered. The first run after
this is deployed, or after a long outage, must not email every alert ever
raised; those were, or will be, in a digest.
"""
import html
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import email_design as design
from app import mail
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS
from app.digest import NEWS_FOR, unsubscribe_url
from app.duplicates import BEING_READ
from app.models import AlertEmailSend, CanonicalSku, Distributor, Invoice, PriceAlert, Tenant, TenantMembership, User
from app.models.enums import AlertStatus

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=48)
SETTLE = timedelta(minutes=15)
REPEAT_QUIET = timedelta(days=7)
LISTED = 8  # per location, before "and N more"


# Everything below works on these plain snapshots, taken before the first
# commit. ORM objects expire on commit, and reloading a PriceAlert afterwards
# is a tenant-scoped query on a session with no tenant bound, which the
# guard in app/db.py refuses: that crashed every run after its first claim.


@dataclass(frozen=True)
class Increase:
    alert_id: uuid.UUID
    tenant_id: uuid.UUID
    sku_id: uuid.UUID
    distributor_id: uuid.UUID | None
    sku: str
    before: Decimal
    now: Decimal
    pct_change: Decimal
    unit: str


@dataclass
class LocationIncreases:
    tenant_id: uuid.UUID
    name: str
    increases: list[Increase] = field(default_factory=list)


@dataclass
class Recipient:
    id: uuid.UUID
    email: str
    name: str
    locations: list[tuple[uuid.UUID, str]] = field(default_factory=list)  # (tenant id, name)


def due_alerts(db: Session, now: datetime) -> dict[uuid.UUID, list[Increase]]:
    """Open, big-enough alerts opened within LOOKBACK about prices still
    recent, by location, biggest first; none for a location whose invoices
    are still arriving."""
    rows = db.execute(
        select(PriceAlert, CanonicalSku.name, CanonicalSku.base_uom, Distributor.name)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .outerjoin(Distributor, Distributor.id == PriceAlert.distributor_id)
        .where(
            PriceAlert.status == AlertStatus.open,
            PriceAlert.pct_change >= Decimal(str(settings.alert_email_min_pct_change)),
            PriceAlert.created_at >= now - LOOKBACK,
            PriceAlert.window_end >= (now - NEWS_FOR).date(),
        )
        .order_by(PriceAlert.tenant_id, PriceAlert.pct_change.desc())
        # Across every location at once: this is the system itself, deciding
        # whom to tell, and each person only gets their own locations' alerts.
        .execution_options(**{TENANT_SCOPE_BYPASS: True})
    ).all()
    still_arriving = set(
        db.scalars(
            select(Invoice.tenant_id)
            .where(
                Invoice.tenant_id.in_(list({alert.tenant_id for alert, *_ in rows})),
                or_(Invoice.status.in_(BEING_READ), Invoice.created_at >= now - SETTLE),
            )
            .distinct()
            .execution_options(**{TENANT_SCOPE_BYPASS: True})
        )
    )
    by_tenant: dict[uuid.UUID, list[Increase]] = {}
    for alert, name, uom, distributor in rows:
        if alert.tenant_id in still_arriving:
            continue  # sent by a later run, if it's still open then
        by_tenant.setdefault(alert.tenant_id, []).append(
            Increase(
                alert_id=alert.id,
                tenant_id=alert.tenant_id,
                sku_id=alert.canonical_sku_id,
                distributor_id=alert.distributor_id,
                sku=design.product_from(name, distributor),
                before=alert.baseline_price,
                now=alert.current_price,
                pct_change=alert.pct_change,
                unit=design.UNIT_LABEL.get(uom.value, uom.value),
            )
        )
    return by_tenant


def _recipients(db: Session, tenant_ids: list[uuid.UUID]) -> list[Recipient]:
    """Active members of these locations who want the emails. Membership, not
    operator access, as for the digest: operators would otherwise hear about
    every location in the system."""
    people: dict[uuid.UUID, Recipient] = {}
    for user_id, email, name, tenant_id, tenant_name in db.execute(
        select(User.id, User.email, User.name, Tenant.id, Tenant.name)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(
            TenantMembership.tenant_id.in_(tenant_ids),
            User.is_active.is_(True),
            User.alert_emails_enabled.is_(True),
        )
        .order_by(User.email, Tenant.name)
    ):
        people.setdefault(user_id, Recipient(user_id, email, name)).locations.append((tenant_id, tenant_name))
    return list(people.values())


def _claim(db: Session, user_id: uuid.UUID, alert_ids: list[uuid.UUID], now: datetime) -> set[uuid.UUID]:
    """Record these alerts as emailed to this person; returns the ones this
    call recorded. Any another run got to first are left out, so each is
    sent once however many runs overlap."""
    if not alert_ids:
        return set()
    claimed = db.execute(
        pg_insert(AlertEmailSend)
        .values([{"id": uuid.uuid4(), "alert_id": a, "user_id": user_id, "sent_at": now} for a in alert_ids])
        .on_conflict_do_nothing(constraint="uq_alert_email_sends_alert_user")
        .returning(AlertEmailSend.alert_id)
    ).scalars()
    result = set(claimed)
    db.commit()
    return result


def _release(db: Session, user_id: uuid.UUID, alert_ids: set[uuid.UUID]) -> None:
    db.rollback()  # whatever failed may have left the transaction unusable
    db.execute(delete(AlertEmailSend).where(AlertEmailSend.user_id == user_id, AlertEmailSend.alert_id.in_(alert_ids)))
    db.commit()


# --- The email ---------------------------------------------------------------


def _subject(locations: list[LocationIncreases]) -> str:
    increases = [i for loc in locations for i in loc.increases]
    where = locations[0].name if len(locations) == 1 else f"{len(locations)} locations"
    if len(increases) == 1:
        (only,) = increases
        return f"Price increase at {where}: {only.sku} up {only.pct_change:.1%}"
    return f"{len(increases)} price increases at {where}"


def compose(user: Recipient, locations: list[LocationIncreases]) -> EmailMessage:
    e = html.escape
    unsubscribe = unsubscribe_url(user.id, "alerts")
    account = f"{settings.public_base_url}/account/password"
    threshold = f"{settings.alert_email_min_pct_change:.0%}"

    text = [f"Hi {user.name},", "", f"Prices just went up by {threshold} or more:", ""]
    sections = []
    for loc in locations:
        shown = loc.increases[:LISTED]
        more = len(loc.increases) - len(shown)
        text += [loc.name, "=" * len(loc.name)]
        text += [design.increase_line(i) for i in shown]
        if more:
            text.append(f"  ...and {more} more")
        text += [f"  {design.dashboard_link('/insights', loc.tenant_id)}", ""]

        sections.append(
            "<div style='margin:20px 0 0;border:1px solid #e5e7eb;border-left:4px solid #fca5a5;border-radius:8px;"
            "padding:16px 18px'>"
            f"<h2 style='font-size:17px;margin:0 0 10px'>{e(loc.name)}</h2>"
            f"{design.increase_table(shown, more)}<p style='margin:12px 0 0'>{design.button(design.dashboard_link('/insights', loc.tenant_id), 'See price alerts')}</p>"
            "</div>"
        )
    text += [
        "Worth raising with your rep before the next order.",
        "",
        "--",
        f"You get these because you have access to {', '.join(loc.name for loc in locations)}.",
        f"Stop these emails (the weekly summary is separate): {unsubscribe}",
    ]

    body = (
        f"<p style='margin:0 0 4px'>Hi {e(user.name)},</p>"
        f"<p style='margin:0;color:{design.MUTED}'>Prices just went up by <strong style='color:{design.RED_TEXT}'>"
        f"{threshold} or more</strong>. Worth raising with your rep before the next order.</p>"
        + "".join(sections)
    )
    footer = (
        "You get these because you have access to these locations. "
        f"<a href='{e(unsubscribe)}' style='color:{design.MUTED}'>Stop price-increase emails</a> "
        "(the weekly summary is separate), or change either from "
        f"<a href='{e(account)}' style='color:{design.MUTED}'>your account</a>."
    )
    return mail.build_message(
        to=user.email,
        subject=_subject(locations),
        text="\n".join(text),
        html=design.document(tag="Price increase", body=body, footer=footer),
        headers={
            "List-Unsubscribe": f"<{unsubscribe}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )


# --- Sending ----------------------------------------------------------------


@dataclass
class AlertEmailRun:
    sent: int = 0
    alerts: int = 0
    failed: list[str] = field(default_factory=list)


def send_alert_emails(db: Session, now: datetime | None = None) -> AlertEmailRun:
    now = now or datetime.now(timezone.utc)
    run = AlertEmailRun()
    by_tenant = due_alerts(db, now)
    if not by_tenant:
        return run
    alert_ids = [i.alert_id for increases in by_tenant.values() for i in increases]
    already = set(
        db.execute(
            select(AlertEmailSend.alert_id, AlertEmailSend.user_id).where(AlertEmailSend.alert_id.in_(alert_ids))
        ).all()
    )
    # (person, location, product, distributor) emailed lately, under any
    # alert. A distributor of None is an alert from before alerts had one,
    # and counts for the product from any distributor.
    recently = set(
        db.execute(
            select(AlertEmailSend.user_id, PriceAlert.tenant_id, PriceAlert.canonical_sku_id, PriceAlert.distributor_id)
            .join(PriceAlert, PriceAlert.id == AlertEmailSend.alert_id)
            .where(AlertEmailSend.sent_at > now - REPEAT_QUIET, AlertEmailSend.alert_id.not_in(alert_ids))
            .execution_options(**{TENANT_SCOPE_BYPASS: True})
        ).all()
    )
    for person in _recipients(db, list(by_tenant)):
        wanted = [
            (tenant_name, increase)
            for tenant_id, tenant_name in person.locations
            for increase in by_tenant[tenant_id]
            if (increase.alert_id, person.id) not in already
            and (person.id, tenant_id, increase.sku_id, increase.distributor_id) not in recently
            and (person.id, tenant_id, increase.sku_id, None) not in recently
        ]
        claimed = _claim(db, person.id, [i.alert_id for _, i in wanted], now)
        if not claimed:
            continue
        try:
            locations: dict[uuid.UUID, LocationIncreases] = {}
            for tenant_name, increase in wanted:
                if increase.alert_id in claimed:
                    locations.setdefault(
                        increase.tenant_id, LocationIncreases(increase.tenant_id, tenant_name)
                    ).increases.append(increase)
            mail.send(compose(person, list(locations.values())))
        except Exception as exc:
            # Anything at all between claiming and sending hands the claims
            # back, or these increases would be recorded as sent and never
            # be: the next run tries again.
            _release(db, person.id, claimed)
            run.failed.append(f"{person.email}: {exc}")
            if isinstance(exc, mail.MailError):
                logger.warning("price-increase email to %s failed: %s", person.email, exc)
            else:
                logger.exception("price-increase email to %s failed", person.email)
            continue
        run.sent += 1
        run.alerts += len(claimed)
    return run
