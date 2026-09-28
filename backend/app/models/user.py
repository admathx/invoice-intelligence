"""Who is signed in, which locations they can see, and what they changed.

Access is granted per location (tenant) through TenantMembership rather than
per business (account): a group's manager typically sees every location, but
a kitchen manager at one location should not see the others, and memberships
express both without a second role system. Operators (`is_operator`) run the
service itself and can see every tenant plus the cross-tenant screens
(Businesses).

None of these tables is TenantScoped. A user spans tenants by design, and the
audit log has cross-tenant rows (regrouping businesses) that the tenant guard
would otherwise hide from the very operators who need to read them. Queries
against them filter explicitly.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Stored lowercased (see app.auth.normalize_email): addresses are
    # case-insensitive in practice, and a second account differing only in
    # case would split one person's history and access across two logins.
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Argon2id (app.auth). Never the password itself, never logged.
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    is_operator: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Deactivating (not deleting) keeps the audit trail's references to what
    # this person did intact after they leave.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Set when an operator issued the password (they've seen it); until the
    # owner picks their own, only the password change is allowed (app.auth).
    password_change_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The weekly email (app/digest.py). Off from the account page or the
    # unsubscribe link in any digest.
    digest_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    # An email the day a big price increase shows up (app/alert_emails.py).
    # Off from the account page or the unsubscribe link in any of them.
    alert_emails_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_tenant_memberships_user_tenant"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class UserSession(Base):
    """A signed-in browser. Stored server-side so it can be revoked (logout,
    deactivation) immediately, which a self-contained signed token can't be.
    Only a SHA-256 of the token is stored: a database leak must not hand out
    working sessions."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True, index=True)


class AuditEvent(Base):
    """Append-only record of who changed what.

    Every correction, confirmation and regrouping here changes numbers other
    businesses read in their benchmarks, so "who did this, and what did it
    say before" has to be answerable. Written in the same transaction as the
    change it describes (app.audit.record), so neither can exist without the
    other.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        # Lookups inside details (migration 0012): an invoice's history finds
        # its lines' events by invoice_id; sign-in throttling counts failures
        # by email.
        Index(
            "ix_audit_events_details_invoice_id",
            text("(details ->> 'invoice_id')"),
            postgresql_where=text("details ? 'invoice_id'"),
        ),
        Index(
            "ix_audit_events_failed_login_email",
            text("(details ->> 'email')"),
            "occurred_at",
            postgresql_where=text("action = 'auth.login_failed'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # clock_timestamp(), not now(): now() is the transaction's start, which
    # gave every event in one transaction the same time (migration 0011).
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.clock_timestamp(), nullable=False, index=True)
    # NULL for the system itself (the extraction worker, email intake).
    # SET NULL rather than cascade: removing a user must never erase the record
    # of what they did.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # NULL for actions that aren't about one location (creating a business).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str] = mapped_column(String, nullable=False)
    # No FK: the entity may later be deleted (a removed line), and its history
    # is exactly what should outlive it.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DigestSend(Base):
    """One weekly digest sent to one person. Unique per (user, week), which is
    what makes sending idempotent (app.digest.send_due_digests)."""

    __tablename__ = "digest_sends"
    __table_args__ = (UniqueConstraint("user_id", "week_of", name="uq_digest_sends_user_week"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # The Monday of the week the digest covers up to.
    week_of: Mapped[date] = mapped_column(Date, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    location_count: Mapped[int] = mapped_column(Integer, nullable=False)


class AlertEmailSend(Base):
    """One price alert emailed to one person. Unique per (alert, user), which
    is what makes the alert emails idempotent (app.alert_emails)."""

    __tablename__ = "alert_email_sends"
    __table_args__ = (UniqueConstraint("alert_id", "user_id", name="uq_alert_email_sends_alert_user"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    alert_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("price_alerts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sent_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)


class PasswordResetToken(Base):
    """A "forgot your password" link (app/api/auth.py). Like sessions, only a
    SHA-256 of the token is stored, so the table can't be read for working
    links. Single use, and short-lived."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(nullable=True)
