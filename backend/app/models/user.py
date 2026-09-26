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
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
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
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)


class AuditEvent(Base):
    """Append-only record of who changed what.

    Every correction, confirmation and regrouping here changes numbers other
    businesses read in their benchmarks, so "who did this, and what did it
    say before" has to be answerable. Written in the same transaction as the
    change it describes (app.audit.record), so neither can exist without the
    other.
    """

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False, index=True)
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
