"""Price-increase emails: the same day a big increase shows up, not Monday.

A price alert opens when a location's recent prices for a product have
climbed clear of its baseline (app/analytics/price_creep.py), which happens
as soon as the invoice that shows it is read or reviewed. The weekly digest
reports every new one; this sends the big ones (settings.
alert_email_min_pct_change and up) straight away, to everyone at that
location who wants them, so a distributor's quiet 15% on a staple can be
queried before the next order rather than a week of orders later.

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

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app import email_design as design
from app import mail
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS
from app.digest import unsubscribe_url
from app.models import AlertEmailSend, CanonicalSku, PriceAlert, Tenant, TenantMembership, User
from app.models.enums import AlertStatus

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(hours=48)
REPEAT_QUIET = timedelta(days=7)
LISTED = 8  # per location, before "and N more"


@dataclass
class Increase:
    alert_id: uuid.UUID
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


def due_alerts(db: Session, now: datetime) -> dict[uuid.UUID, list[tuple[PriceAlert, str, str]]]:
    """Open, big-enough alerts opened within LOOKBACK, by location, biggest first."""
    rows = db.execute(
        select(PriceAlert, CanonicalSku.name, CanonicalSku.base_uom)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .where(
            PriceAlert.status == AlertStatus.open,
            PriceAlert.pct_change >= Decimal(str(settings.alert_email_min_pct_change)),
            PriceAlert.created_at >= now - LOOKBACK,
        )
        .order_by(PriceAlert.tenant_id, PriceAlert.pct_change.desc())
        # Across every location at once: this is the system itself, deciding
        # whom to tell, and each person only gets their own locations' alerts.
        .execution_options(**{TENANT_SCOPE_BYPASS: True})
    ).all()
    by_tenant: dict[uuid.UUID, list[tuple[PriceAlert, str, str]]] = {}
    for alert, name, uom in rows:
        by_tenant.setdefault(alert.tenant_id, []).append((alert, name, design.UNIT_LABEL.get(uom.value, uom.value)))
    return by_tenant


def _recipients(db: Session, tenant_ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[User, list[Tenant]]]:
    """Active members of these locations who want the emails. Membership, not
    operator access, as for the digest: operators would otherwise hear about
    every location in the system."""
    people: dict[uuid.UUID, tuple[User, list[Tenant]]] = {}
    for user, tenant in db.execute(
        select(User, Tenant)
        .join(TenantMembership, TenantMembership.user_id == User.id)
        .join(Tenant, Tenant.id == TenantMembership.tenant_id)
        .where(
            TenantMembership.tenant_id.in_(tenant_ids),
            User.is_active.is_(True),
            User.alert_emails_enabled.is_(True),
        )
        .order_by(User.email, Tenant.name)
    ):
        people.setdefault(user.id, (user, []))[1].append(tenant)
    return people


def _claim(db: Session, user: User, alert_ids: list[uuid.UUID], now: datetime) -> set[uuid.UUID]:
    """Record these alerts as emailed to this person; returns the ones this
    call recorded. Any another run got to first are left out, so each is
    sent once however many runs overlap."""
    if not alert_ids:
        return set()
    claimed = db.execute(
        pg_insert(AlertEmailSend)
        .values([{"id": uuid.uuid4(), "alert_id": a, "user_id": user.id, "sent_at": now} for a in alert_ids])
        .on_conflict_do_nothing(constraint="uq_alert_email_sends_alert_user")
        .returning(AlertEmailSend.alert_id)
    ).scalars()
    result = set(claimed)
    db.commit()
    return result


def _release(db: Session, user: User, alert_ids: set[uuid.UUID]) -> None:
    db.execute(delete(AlertEmailSend).where(AlertEmailSend.user_id == user.id, AlertEmailSend.alert_id.in_(alert_ids)))
    db.commit()


# --- The email ---------------------------------------------------------------


def _subject(locations: list[LocationIncreases]) -> str:
    increases = [i for loc in locations for i in loc.increases]
    where = locations[0].name if len(locations) == 1 else f"{len(locations)} locations"
    if len(increases) == 1:
        (only,) = increases
        return f"Price increase at {where}: {only.sku} up {only.pct_change:.1%}"
    return f"{len(increases)} price increases at {where}"


def compose(user: User, locations: list[LocationIncreases]) -> EmailMessage:
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
        text += [f"  - {i.sku}: {design.price_per(i.before, i.unit)} -> {design.price_per(i.now, i.unit)} (+{i.pct_change:.1%})" for i in shown]
        if more:
            text.append(f"  ...and {more} more")
        text += [f"  {design.dashboard_link('/insights', loc.tenant_id)}", ""]

        rows = "".join(
            "<tr>"
            f"<td style='padding:4px 12px 4px 0'>{e(i.sku)}</td>"
            f"<td style='padding:4px 12px 4px 0;color:{design.MUTED}'><span style='white-space:nowrap'>{e(design.price_per(i.before, i.unit))} &rarr;</span> "
            f"<strong style='color:#111827;white-space:nowrap'>{e(design.price_per(i.now, i.unit))}</strong></td>"
            f"<td style='padding:4px 0;text-align:right'>{design.pill(f'▲ +{i.pct_change:.1%}', design.RED_TEXT, design.RED_TINT)}</td>"
            "</tr>"
            for i in shown
        )
        more_html = f"<p style='margin:4px 0 0;color:{design.MUTED}'>&hellip;and {more} more</p>" if more else ""
        sections.append(
            "<div style='margin:20px 0 0;border:1px solid #e5e7eb;border-left:4px solid #fca5a5;border-radius:8px;"
            "padding:16px 18px'>"
            f"<h2 style='font-size:17px;margin:0 0 10px'>{e(loc.name)}</h2>"
            f"<table role='presentation' cellpadding='0' cellspacing='0' style='font-size:14px;border-collapse:collapse'>{rows}</table>"
            f"{more_html}<p style='margin:12px 0 0'>{design.button(design.dashboard_link('/insights', loc.tenant_id), 'See it on Insights')}</p>"
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
    alert_ids = [a.id for rows in by_tenant.values() for a, _, _ in rows]
    already = set(
        db.execute(
            select(AlertEmailSend.alert_id, AlertEmailSend.user_id).where(AlertEmailSend.alert_id.in_(alert_ids))
        ).all()
    )
    # (person, location, product) emailed lately, under any alert.
    recently = set(
        db.execute(
            select(AlertEmailSend.user_id, PriceAlert.tenant_id, PriceAlert.canonical_sku_id)
            .join(PriceAlert, PriceAlert.id == AlertEmailSend.alert_id)
            .where(AlertEmailSend.sent_at > now - REPEAT_QUIET, AlertEmailSend.alert_id.not_in(alert_ids))
            .execution_options(**{TENANT_SCOPE_BYPASS: True})
        ).all()
    )
    for user, tenants in _recipients(db, list(by_tenant)).values():
        wanted = [
            (tenant, alert, name, unit)
            for tenant in tenants
            for alert, name, unit in by_tenant[tenant.id]
            if (alert.id, user.id) not in already
            and (user.id, tenant.id, alert.canonical_sku_id) not in recently
        ]
        claimed = _claim(db, user, [alert.id for _, alert, _, _ in wanted], now)
        if not claimed:
            continue
        locations: dict[uuid.UUID, LocationIncreases] = {}
        for tenant, alert, name, unit in wanted:
            if alert.id in claimed:
                locations.setdefault(tenant.id, LocationIncreases(tenant.id, tenant.name)).increases.append(
                    Increase(alert.id, name, alert.baseline_price, alert.current_price, alert.pct_change, unit)
                )
        try:
            mail.send(compose(user, list(locations.values())))
            run.sent += 1
            run.alerts += len(claimed)
        except mail.MailError as exc:
            _release(db, user, claimed)
            run.failed.append(f"{user.email}: {exc}")
            logger.warning("price-increase email to %s failed: %s", user.email, exc)
    return run
