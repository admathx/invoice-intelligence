import uuid
from collections.abc import Generator
from datetime import datetime

from sqlalchemy import ForeignKey, create_engine, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, with_loader_criteria
from sqlalchemy.types import DateTime

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

TENANT_SCOPE_BYPASS = "tenant_scope_bypass"
_TENANT_ID_INFO_KEY = "tenant_id"


class Base(DeclarativeBase):
    # All timestamps are UTC/timestamptz per SPEC.md §11 — set once here rather
    # than repeating DateTime(timezone=True) on every Mapped[datetime] column.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class TenantScoped:
    """Declarative mixin providing tenant_id: every model that mixes this in
    (alongside Base) gets its own real tenant_id column (FK to tenants.id) —
    SQLAlchemy copies a mixin's mapped_column() declarations onto each subclass's
    own table, so this doesn't create a shared column across models.

    SPEC.md §11: "Every query filters on tenant_id; add a session-level guard so
    it can't be forgotten." _enforce_tenant_scope below auto-injects a tenant_id
    filter into every SELECT against a TenantScoped entity when the owning
    Session has a tenant bound (see get_db_for_tenant/bind_tenant), and raises
    instead of silently running unscoped when no tenant is bound at all — turning
    "forgot to filter by tenant_id" from a silent cross-tenant data leak into an
    immediate, loud error. (with_loader_criteria's callable receives this mixin
    itself as `cls`, which is why tenant_id has to be a real mapped attribute
    here rather than just documented as "each subclass declares one.")
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )


def bind_tenant(db: Session, tenant_id: uuid.UUID) -> None:
    """Scope every TenantScoped query on this Session to tenant_id.

    Deliberately stored on Session.info (a plain dict on the Session instance)
    rather than a contextvars.ContextVar: FastAPI runs sync dependencies and sync
    path-operation functions via anyio's thread-pool executor, and each such call
    can get its own copied contextvars Context — a value set in a dependency
    isn't reliably visible in the endpoint body, and a Token captured before
    `yield` isn't valid for reset() after it (different Context). Session.info
    has none of that: it's a regular attribute on the same Session object that
    FastAPI's dependency injection hands to the endpoint by reference, so it
    travels correctly regardless of which thread ran which part.
    """
    db.info[_TENANT_ID_INFO_KEY] = tenant_id


@event.listens_for(Session, "do_orm_execute")
def _enforce_tenant_scope(execute_state) -> None:
    if not execute_state.is_select or execute_state.execution_options.get(TENANT_SCOPE_BYPASS):
        return

    tenant_id = execute_state.session.info.get(_TENANT_ID_INFO_KEY)
    if tenant_id is not None:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(TenantScoped, lambda cls: cls.tenant_id == tenant_id, include_aliases=True)
        )
        return

    for desc in execute_state.statement.column_descriptions:
        entity = desc.get("entity")
        if isinstance(entity, type) and issubclass(entity, TenantScoped):
            raise RuntimeError(
                f"Query against {entity.__name__} executed on a Session with no tenant "
                "bound (see app.db.bind_tenant). Use the get_db_for_tenant dependency, "
                "call bind_tenant explicitly, or pass "
                f'execution_options({TENANT_SCOPE_BYPASS}=True) for a deliberate '
                "cross-tenant/admin query."
            )


def get_db() -> Generator[Session, None, None]:
    """Unscoped session — only for endpoints/scripts with no tenant-scoped query."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_for_tenant(tenant_id: uuid.UUID) -> Generator[Session, None, None]:
    """FastAPI dependency: scopes every TenantScoped query in this request to tenant_id."""
    db = SessionLocal()
    bind_tenant(db, tenant_id)
    try:
        yield db
    finally:
        db.close()
