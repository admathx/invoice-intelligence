"""Development logins: one operator, and one member of a single location.

Passwords are generated fresh on every run and written to
backend/dev_users.local.json (gitignored), which is where to look them up.
Re-running resets both passwords and signs out their existing sessions.

Refuses to run against anything but a local database: these are throwaway
credentials, and a copy of them sitting in a file is fine only because the
database they open is on this machine.

    python scripts/seed_dev_users.py [MEMBER_TENANT_ID]

MEMBER_TENANT_ID defaults to the tenant with the most invoices, which is the
one the seeded corpus makes interesting to look at.

upsert_login is also how `make smoke` and the Playwright fixture sign in:
each makes its own throwaway login with a password that exists only for the
length of the run.
"""
import json
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url

from app.auth import hash_password
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal
from app.models import Invoice, Tenant, TenantMembership, User, UserSession

OUTPUT = Path(__file__).resolve().parents[1] / "dev_users.local.json"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

OPERATOR = ("operator@dev.test", "Dev Operator")
MEMBER = ("member@dev.test", "Dev Member")


def require_local_database() -> None:
    if make_url(settings.database_url).host not in LOCAL_HOSTS:
        sys.exit("refusing: DATABASE_URL is not a local database")


def upsert_login(
    db, email: str, name: str, *, operator: bool = False, tenant_ids: tuple[uuid.UUID, ...] = ()
) -> tuple[User, str]:
    """Create or reset a local login with a fresh random password and access to
    tenant_ids (added to, never removed from). Commits."""
    require_local_database()
    password = secrets.token_urlsafe(12)
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(id=uuid.uuid4(), email=email, name=name, password_hash="", is_operator=operator)
        db.add(user)
    user.password_hash = hash_password(password)
    user.is_operator = operator
    user.is_active = True
    db.flush()
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    for tenant_id in tenant_ids:
        if not db.scalar(
            select(TenantMembership.id).where(
                TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant_id
            )
        ):
            db.add(TenantMembership(user_id=user.id, tenant_id=tenant_id))
    db.commit()
    return user, password


def main() -> None:
    require_local_database()

    db = SessionLocal()
    try:
        if len(sys.argv) > 1:
            tenant = db.get(Tenant, uuid.UUID(sys.argv[1]))
        else:
            tenant = db.scalar(
                select(Tenant)
                .join(Invoice, Invoice.tenant_id == Tenant.id)
                .group_by(Tenant.id)
                .order_by(func.count(Invoice.id).desc())
                .limit(1)
                .execution_options(**{TENANT_SCOPE_BYPASS: True})
            )
        if tenant is None:
            sys.exit("no tenant to give the member access to — run `make seed` first, or pass a tenant id")

        operator, operator_password = upsert_login(db, *OPERATOR, operator=True)
        member, member_password = upsert_login(db, *MEMBER, tenant_ids=(tenant.id,))

        OUTPUT.write_text(
            json.dumps(
                {
                    "operator": {"email": operator.email, "password": operator_password},
                    "member": {
                        "email": member.email,
                        "password": member_password,
                        "location": {"id": str(tenant.id), "name": tenant.name},
                    },
                },
                indent=2,
            )
            + "\n"
        )
        OUTPUT.chmod(0o600)
        print(f"dev logins written to {OUTPUT}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
