"""Create users and manage their access. There is no sign-up page: an operator
creates each login here.

    python scripts/manage_users.py create EMAIL "Full Name" [--operator] [--location TENANT_ID ...]
    python scripts/manage_users.py grant EMAIL TENANT_ID
    python scripts/manage_users.py revoke EMAIL TENANT_ID
    python scripts/manage_users.py set-password EMAIL
    python scripts/manage_users.py deactivate EMAIL
    python scripts/manage_users.py list

Passwords are prompted for (never taken as an argument, which would land in
shell history). Pipe one in on stdin to script it.

Every change is recorded in the audit trail as a system action, since the
person at this terminal isn't a signed-in user.
"""
import argparse
import getpass
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app import audit
from app.auth import hash_password, normalize_email
from app.db import SessionLocal
from app.models import Tenant, TenantMembership, User, UserSession

MIN_PASSWORD_LENGTH = 12


def _read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("passwords don't match")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _user(db, email: str) -> User:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        sys.exit(f"no user {email}")
    return user


def _tenant(db, tenant_id: str) -> Tenant:
    tenant = db.get(Tenant, uuid.UUID(tenant_id))
    if tenant is None:
        sys.exit(f"no tenant {tenant_id}")
    return tenant


def _revoke_sessions(db, user: User) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )


def cmd_create(db, args) -> None:
    email = normalize_email(args.email)
    if db.scalar(select(User.id).where(User.email == email)):
        sys.exit(f"{email} already exists")
    tenants = [_tenant(db, t) for t in args.location]
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=args.name.strip(),
        password_hash=hash_password(_read_password()),
        is_operator=args.operator,
    )
    db.add(user)
    db.flush()
    audit.record(db, None, "user.created", "user", user.id, email=email, is_operator=args.operator)
    for tenant in tenants:
        db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id))
        audit.record(db, None, "user.access_granted", "user", user.id, tenant.id, email=email)
    db.commit()
    print(f"created {email}{' (operator)' if args.operator else ''} with access to {len(tenants)} location(s)")


def cmd_grant(db, args) -> None:
    user, tenant = _user(db, args.email), _tenant(db, args.tenant_id)
    exists = db.scalar(
        select(TenantMembership.id).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant.id)
    )
    if not exists:
        db.add(TenantMembership(user_id=user.id, tenant_id=tenant.id))
        audit.record(db, None, "user.access_granted", "user", user.id, tenant.id, email=user.email)
        db.commit()
    print(f"{user.email} can open {tenant.name}")


def cmd_revoke(db, args) -> None:
    user, tenant = _user(db, args.email), _tenant(db, args.tenant_id)
    removed = db.execute(
        delete(TenantMembership).where(TenantMembership.user_id == user.id, TenantMembership.tenant_id == tenant.id)
    ).rowcount
    if removed:
        audit.record(db, None, "user.access_revoked", "user", user.id, tenant.id, email=user.email)
        db.commit()
    print(f"{user.email} can no longer open {tenant.name}")


def cmd_set_password(db, args) -> None:
    user = _user(db, args.email)
    user.password_hash = hash_password(_read_password())
    # Anyone signed in with the old password is signed out.
    _revoke_sessions(db, user)
    audit.record(db, None, "user.password_changed", "user", user.id, email=user.email)
    db.commit()
    print(f"password changed for {user.email}; existing sessions signed out")


def cmd_deactivate(db, args) -> None:
    user = _user(db, args.email)
    user.is_active = False
    _revoke_sessions(db, user)
    audit.record(db, None, "user.deactivated", "user", user.id, email=user.email)
    db.commit()
    print(f"deactivated {user.email}")


def cmd_list(db, args) -> None:
    for user in db.scalars(select(User).order_by(User.email)):
        count = len(list(db.scalars(select(TenantMembership.id).where(TenantMembership.user_id == user.id))))
        flags = [f for f, on in (("operator", user.is_operator), ("inactive", not user.is_active)) if on]
        print(f"{user.email:40} {user.name:30} {count:3} location(s) {' '.join(flags)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("email")
    create.add_argument("name")
    create.add_argument("--operator", action="store_true")
    create.add_argument("--location", action="append", default=[], metavar="TENANT_ID")
    for name in ("grant", "revoke"):
        p = sub.add_parser(name)
        p.add_argument("email")
        p.add_argument("tenant_id")
    for name in ("set-password", "deactivate"):
        sub.add_parser(name).add_argument("email")
    sub.add_parser("list")

    args = parser.parse_args()
    commands = {
        "create": cmd_create,
        "grant": cmd_grant,
        "revoke": cmd_revoke,
        "set-password": cmd_set_password,
        "deactivate": cmd_deactivate,
        "list": cmd_list,
    }
    db = SessionLocal()
    try:
        commands[args.command](db, args)
    finally:
        db.close()


if __name__ == "__main__":
    main()
