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
import sys
import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.users import create_user, generate_password, grant_access, set_password, update_user
from app.config import settings
from app.db import TENANT_SCOPE_BYPASS, SessionLocal
from app.models import Invoice, Tenant, User

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
    tenant_ids (added to, never removed from). Commits.

    Goes through app/users.py like every other change to a login, so these
    grants and resets appear in the audit trail too (as the system)."""
    require_local_database()
    password = generate_password()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        user = create_user(db, None, email=email, name=name, password=password, is_operator=operator)
    else:
        set_password(db, None, user, password)  # also signs out its old sessions
        update_user(db, None, user, is_operator=operator, is_active=True)
        # Back to defaults, like the password: a run that stopped halfway
        # (a failed test that had just unsubscribed) mustn't shape the next.
        user.digest_enabled = True
    for tenant_id in tenant_ids:
        grant_access(db, None, user, tenant_id)
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
