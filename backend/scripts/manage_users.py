"""Create users and manage their access from the command line. Operators can
do the same from the Users screen; both go through app/users.py.

    python scripts/manage_users.py create EMAIL "Full Name" [--operator] [--location TENANT_ID ...]
    python scripts/manage_users.py grant EMAIL TENANT_ID
    python scripts/manage_users.py revoke EMAIL TENANT_ID
    python scripts/manage_users.py set-password EMAIL
    python scripts/manage_users.py deactivate EMAIL
    python scripts/manage_users.py prune-sessions
    python scripts/manage_users.py list

Passwords are prompted for (never taken as an argument, which would land in
shell history). Pipe one in on stdin to script it. This is how the first
operator gets created, since the screen needs an operator to sign in.

Every change is recorded in the audit trail as a system action, since the
person at this terminal isn't a signed-in user.
"""
import argparse
import getpass
import sys
import uuid

from sqlalchemy import func, select

from app import users as user_service
from app.auth import normalize_email, prune_sessions
from app.db import SessionLocal
from app.models import TenantMembership, User


def _read_password() -> str:
    if sys.stdin.isatty():
        password = getpass.getpass("Password: ")
        if getpass.getpass("Again: ") != password:
            sys.exit("passwords don't match")
        return password
    return sys.stdin.readline().rstrip("\n")


def _user(db, email: str) -> User:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        sys.exit(f"no user {email}")
    return user


def cmd_create(db, args) -> str:
    user = user_service.create_user(
        db,
        None,
        email=args.email,
        name=args.name,
        password=_read_password(),
        is_operator=args.operator,
        tenant_ids=[uuid.UUID(t) for t in args.location],
    )
    return f"created {user.email}{' (operator)' if user.is_operator else ''} with access to {len(args.location)} location(s)"


def cmd_grant(db, args) -> str:
    user = _user(db, args.email)
    user_service.grant_access(db, None, user, uuid.UUID(args.tenant_id))
    return f"{user.email} can open {args.tenant_id}"


def cmd_revoke(db, args) -> str:
    user = _user(db, args.email)
    user_service.revoke_access(db, None, user, uuid.UUID(args.tenant_id))
    return f"{user.email} can no longer open {args.tenant_id}"


def cmd_set_password(db, args) -> str:
    user = _user(db, args.email)
    user_service.set_password(db, None, user, _read_password())
    return f"password changed for {user.email}; existing sessions signed out"


def cmd_deactivate(db, args) -> str:
    user = _user(db, args.email)
    user_service.update_user(db, None, user, is_active=False)
    return f"deactivated {user.email}"


def cmd_prune_sessions(db, args) -> str:
    return f"deleted {prune_sessions(db)} dead session(s)"


def cmd_list(db, args) -> str:
    counts = dict(
        db.execute(select(TenantMembership.user_id, func.count()).group_by(TenantMembership.user_id)).all()
    )
    rows = []
    for user in db.scalars(select(User).order_by(User.email)):
        flags = [f for f, on in (("operator", user.is_operator), ("inactive", not user.is_active)) if on]
        rows.append(f"{user.email:40} {user.name:30} {counts.get(user.id, 0):3} location(s) {' '.join(flags)}")
    return "\n".join(rows)


COMMANDS = {
    "create": cmd_create,
    "grant": cmd_grant,
    "revoke": cmd_revoke,
    "set-password": cmd_set_password,
    "deactivate": cmd_deactivate,
    "prune-sessions": cmd_prune_sessions,
    "list": cmd_list,
}


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
    sub.add_parser("prune-sessions")
    sub.add_parser("list")

    args = parser.parse_args()
    db = SessionLocal()
    try:
        message = COMMANDS[args.command](db, args)
        db.commit()
        print(message)
    except user_service.UserError as exc:
        sys.exit(str(exc))
    finally:
        db.close()


if __name__ == "__main__":
    main()
