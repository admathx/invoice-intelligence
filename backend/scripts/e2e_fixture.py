"""Playwright e2e fixture (Phase 5 gate): creates a small, deterministic
review-queue scenario without spending Anthropic budget on real extraction —
same "ground truth stands in for extraction" reasoning as
seed_corpus_pipeline.py, just for two hand-built line items instead of the
whole corpus.

Subcommands (see backend/scripts/e2e_fixture.py --help via argparse):
  setup <tenant_id>          creates 2 pending review-queue line items, prints
                             their ids and identifying fields as JSON.
  setup-bulk <tenant_id> <n> creates n pending items that each already have a
                             suggested canonical_sku_id (the review-band
                             case) — for timing "clear N items via keyboard."
  login <tenant_id>          creates/resets the e2e login (a member of that
                             tenant) with a fresh random password, prints
                             {email, password} for the spec to sign in with.
  login-operator             the same for an e2e operator login.
  cleanup-users              deletes the logins the Users-screen test created
                             (e2e-new-*@dev.test) and their history.
  pick-tenant                prints the id of the seeded location with the most
                             invoices (CI has no .env.local to name one).
  cleanup-locations          deletes the locations the Businesses test created
                             ("E2E Location *") and their history.
  reset-link <tenant_id>     creates/resets a separate e2e login (its password
                             is about to be changed) and prints {email, token}
                             for a "forgot your password" link, as if emailed.
  verify-alias <distributor_id> <raw_sku> <raw_description>
                             calls the real matcher again for that
                             (distributor, raw_sku) pair and prints the
                             MatchResult as JSON — this is what "the next
                             matching line auto-resolves" means functionally.
"""
import argparse
import json
import sys
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sqlalchemy  # noqa: E402

from app.analytics.price_creep import upsert_creep_alerts  # noqa: E402
from app.db import SessionLocal, bind_tenant  # noqa: E402
from app.models import (  # noqa: E402
    AuditEvent,
    CanonicalSku,
    Distributor,
    Invoice,
    InvoiceLineItem,
    PriceObservation,
    SkuAlias,
    Tenant,
    TenantMembership,
    User,
)
from app.models.enums import InvoiceSource, InvoiceStatus, ReviewStatus  # noqa: E402
from app.normalize.matcher import match_line_item  # noqa: E402


def _delete_history(db, invoice_ids: list[uuid.UUID]) -> None:
    """The fixtures' audit events go with them: a history for invoices that
    no longer exist would only clutter the location's Activity page."""
    ids = [str(i) for i in invoice_ids]
    db.execute(
        sqlalchemy.delete(AuditEvent).where(
            sqlalchemy.or_(AuditEvent.entity_id.in_(invoice_ids), AuditEvent.details["invoice_id"].astext.in_(ids))
        )
    )


def _cleanup_prior_fixtures(db, tenant_id: uuid.UUID) -> None:
    """Each run creates a fresh, uuid-tagged distributor for isolation —
    without this, repeated `npx playwright test` runs (e.g. in CI) would
    accumulate one throwaway distributor/invoice per run forever.

    Scoped to THIS tenant_id (via a join through Invoice), not just the
    "e2e-" slug prefix alone: an unscoped delete-by-slug-prefix would let
    two Playwright shards running concurrently against different tenants
    delete each other's in-progress fixture rows mid-test — a code-review
    finding on Phase 5.
    """
    prior_ids = [
        row[0]
        for row in db.execute(
            sqlalchemy.select(Distributor.id)
            .join(Invoice, Invoice.distributor_id == Distributor.id)
            .where(Distributor.slug.like("e2e-%"), Invoice.tenant_id == tenant_id)
            .distinct()
        ).all()
    ]
    # The empty-invoice fixture uses the tenant's REAL distributor (so its
    # suggestions come from real purchase history), which the slug-based
    # sweep below can't see. Found by its invoice number instead.
    empty_ids = [
        row[0]
        for row in db.execute(
            sqlalchemy.select(Invoice.id).where(Invoice.tenant_id == tenant_id, Invoice.invoice_number.like("E2E-EMPTY-%"))
        ).all()
    ]
    if empty_ids:
        _delete_history(db, empty_ids)
        empty_lines = sqlalchemy.select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id.in_(empty_ids))
        db.execute(sqlalchemy.delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(empty_lines)))
        db.execute(sqlalchemy.delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id.in_(empty_ids)))
        db.execute(sqlalchemy.delete(Invoice).where(Invoice.id.in_(empty_ids)))
        db.commit()
    if not prior_ids:
        return
    invoice_ids = [
        row[0] for row in db.execute(sqlalchemy.select(Invoice.id).where(Invoice.distributor_id.in_(prior_ids))).all()
    ]
    _delete_history(db, invoice_ids)
    line_item_ids = sqlalchemy.select(InvoiceLineItem.id).where(InvoiceLineItem.invoice_id.in_(invoice_ids))
    db.execute(sqlalchemy.delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_item_ids)))
    db.execute(sqlalchemy.delete(InvoiceLineItem).where(InvoiceLineItem.invoice_id.in_(invoice_ids)))
    db.execute(sqlalchemy.delete(Invoice).where(Invoice.id.in_(invoice_ids)))
    db.execute(sqlalchemy.delete(SkuAlias).where(SkuAlias.distributor_id.in_(prior_ids)))
    db.execute(sqlalchemy.delete(Distributor).where(Distributor.id.in_(prior_ids)))
    db.commit()


def cmd_setup(tenant_id: str) -> None:
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db, uuid.UUID(tenant_id))

    tag = uuid.uuid4().hex[:8]
    distributor = Distributor(id=uuid.uuid4(), name=f"E2E Distributor {tag}", slug=f"e2e-test-{tag}")
    db.add(distributor)

    avocado = db.scalar(sqlalchemy.select(CanonicalSku).where(CanonicalSku.name == "Avocado"))

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor.id,
        invoice_number=f"E2E-{tag}",
        # A fixed, early date (not None): the review queue orders by
        # invoice_date ascending, and Postgres sorts NULLs last — an
        # unset date would put this fixture's items behind any real,
        # already-pending corpus review items instead of always first.
        invoice_date=date(2020, 1, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    db.add(invoice)

    confirm_sku = f"E2E-CONFIRM-{tag}"
    confirm_item = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        invoice_id=invoice.id,
        line_number=1,
        raw_description=f"E2E Confirm Test Item {tag}",
        raw_sku=confirm_sku,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("10.00"),
        extended_price=Decimal("10.00"),
        uom="EA",
        canonical_sku_id=avocado.id if avocado else None,
        match_confidence=Decimal("0.85"),
        review_status=ReviewStatus.pending,
    )
    db.add(confirm_item)

    correct_sku = f"E2E-CORRECT-{tag}"
    correct_item = InvoiceLineItem(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        invoice_id=invoice.id,
        line_number=2,
        raw_description=f"E2E Correct Test Item {tag}",
        raw_sku=correct_sku,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("5.00"),
        extended_price=Decimal("5.00"),
        uom="EA",
        canonical_sku_id=None,
        review_status=ReviewStatus.pending,
    )
    db.add(correct_item)
    db.commit()

    print(
        json.dumps(
            {
                "distributor_id": str(distributor.id),
                "confirm_line_id": str(confirm_item.id),
                "confirm_raw_description": confirm_item.raw_description,
                "correct_line_id": str(correct_item.id),
                "correct_raw_sku": correct_sku,
                "correct_raw_description": correct_item.raw_description,
            }
        )
    )


def cmd_setup_bulk(tenant_id: str, count: int) -> None:
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db, uuid.UUID(tenant_id))

    tag = uuid.uuid4().hex[:8]
    distributor = Distributor(id=uuid.uuid4(), name=f"E2E Bulk Distributor {tag}", slug=f"e2e-bulk-{tag}")
    db.add(distributor)

    # Oldest-first: the real ~200-item catalog was seeded in Phase 1, before
    # any test run could have created a stray CanonicalSku row of its own —
    # picking newest-first (or unordered) risks a match against test debris
    # instead of a real catalog item.
    catalog = list(db.scalars(sqlalchemy.select(CanonicalSku).order_by(CanonicalSku.created_at).limit(count)))
    if len(catalog) < count:
        raise SystemExit(f"catalog has only {len(catalog)} SKUs, need {count}")

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor.id,
        invoice_number=f"E2E-BULK-{tag}",
        invoice_date=date(2020, 1, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.extracted,
    )
    db.add(invoice)

    line_ids = []
    for i, sku in enumerate(catalog):
        line = InvoiceLineItem(
            id=uuid.uuid4(),
            tenant_id=uuid.UUID(tenant_id),
            invoice_id=invoice.id,
            line_number=i + 1,
            raw_description=f"E2E Bulk Item {tag} #{i}",
            raw_sku=f"E2E-BULK-{tag}-{i}",
            raw_pack_size="1 EA",
            quantity=Decimal("1"),
            unit_price=Decimal("10.00"),
            extended_price=Decimal("10.00"),
            uom="EA",
            canonical_sku_id=sku.id,
            match_confidence=Decimal("0.85"),
            review_status=ReviewStatus.pending,
        )
        db.add(line)
        line_ids.append(str(line.id))
    db.commit()

    print(json.dumps({"distributor_id": str(distributor.id), "invoice_date": "2020-01-01", "line_ids": line_ids}))


def cmd_setup_needs_review(tenant_id: str) -> None:
    """An invoice the worker would have sent to needs_review: line 1's unit
    price was misread as $74.50 when the page says $47.50, so 2 x 74.50 is not
    the printed $95.00 extended. Exercises the invoice review screen, which
    synthetic data never reaches on its own (it never fails arithmetic).
    """
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db, uuid.UUID(tenant_id))

    tag = uuid.uuid4().hex[:8]
    distributor = Distributor(id=uuid.uuid4(), name=f"E2E Review Distributor {tag}", slug=f"e2e-review-{tag}")
    db.add(distributor)
    mozzarella = db.scalar(sqlalchemy.select(CanonicalSku).where(CanonicalSku.name == "Mozzarella Shredded Whole Milk"))
    avocado = db.scalar(sqlalchemy.select(CanonicalSku).where(CanonicalSku.name == "Avocado"))

    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor.id,
        invoice_number=f"E2E-REVIEW-{tag}",
        invoice_date=date(2020, 1, 2),
        subtotal=Decimal("142.50"),
        tax=Decimal("0.00"),
        total=Decimal("142.50"),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.needs_review,
    )
    db.add(invoice)
    for number, sku, unit, extended, normalized in (
        (1, mozzarella, "74.50", "95.00", "3.7250"),
        (2, avocado, "47.50", "47.50", "2.3750"),
    ):
        qty = Decimal("2") if number == 1 else Decimal("1")
        db.add(
            InvoiceLineItem(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(tenant_id),
                invoice_id=invoice.id,
                line_number=number,
                raw_description=f"E2E REVIEW LINE {number}",
                raw_sku=f"E2E-REVIEW-{tag}-{number}",
                raw_pack_size="4/5 LB",
                quantity=qty,
                unit_price=Decimal(unit),
                extended_price=Decimal(extended),
                uom="CS",
                canonical_sku_id=sku.id,
                normalized_qty_base=qty * 20,
                normalized_unit_price=Decimal(normalized),
                base_uom=sku.base_uom,
                review_status=ReviewStatus.auto,
            )
        )
    db.commit()
    print(json.dumps({"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number}))


def cmd_setup_empty_invoice(tenant_id: str) -> None:
    """An invoice where extraction found no line items at all, from the
    tenant's own most-used distributor, so the add-line modal's suggestions
    are this tenant's real purchase history."""
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db, uuid.UUID(tenant_id))

    distributor_id = db.execute(
        sqlalchemy.select(Invoice.distributor_id, sqlalchemy.func.count())
        .join(Distributor, Distributor.id == Invoice.distributor_id)
        .where(Invoice.tenant_id == uuid.UUID(tenant_id), Invoice.distributor_id.is_not(None))
        .group_by(Invoice.distributor_id, Distributor.slug)
        .order_by(sqlalchemy.func.count().desc(), Distributor.slug)  # ties by slug, not at random
        .limit(1)
    ).scalar()
    # Something this location really bought from that distributor, for the
    # test to search its history for, rather than assuming what's in it.
    description = db.scalar(
        sqlalchemy.select(InvoiceLineItem.raw_description)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(
            Invoice.tenant_id == uuid.UUID(tenant_id),
            Invoice.distributor_id == distributor_id,
            Invoice.status.in_([InvoiceStatus.extracted, InvoiceStatus.confirmed]),
        )
        .order_by(Invoice.invoice_date.desc(), InvoiceLineItem.raw_description)
        .limit(1)
    )
    search = next((w for w in (description or "").split() if w.isalpha() and len(w) >= 3), None)
    if search is None:
        sys.exit("this location has no purchase history to search: run the corpus pipeline first")
    tag = uuid.uuid4().hex[:8]
    invoice = Invoice(
        id=uuid.uuid4(),
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=distributor_id,
        invoice_number=f"E2E-EMPTY-{tag}",
        invoice_date=date(2026, 9, 1),
        source=InvoiceSource.upload,
        original_file_uri="file:///dev/null",
        status=InvoiceStatus.needs_review,
    )
    db.add(invoice)
    db.commit()
    print(json.dumps({"invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number, "search": search}))


def cmd_cleanup(tenant_id: str) -> None:
    """Remove every fixture this script created for the tenant, and refresh
    its creep alerts. Run after the suite, not only before the next one: the
    fixtures confirm invoices whose lines are real catalog SKUs in the tenant's
    real metro, so until they're gone their prices sit in real benchmark cells
    and creep windows."""
    db = SessionLocal()
    bind_tenant(db, uuid.UUID(tenant_id))
    _cleanup_prior_fixtures(db, uuid.UUID(tenant_id))
    upsert_creep_alerts(db, uuid.UUID(tenant_id))
    print(json.dumps({"cleaned": tenant_id}))


E2E_LOGIN = ("e2e@dev.test", "E2E Reviewer")
E2E_OPERATOR = ("e2e-operator@dev.test", "E2E Operator")
# The password-reset test changes this login's password, so it isn't E2E_LOGIN.
E2E_RESET_LOGIN = ("e2e-reset@dev.test", "E2E Reset")
# Logins the Users-screen test creates through the UI.
E2E_CREATED_PATTERN = "e2e-new-%@dev.test"
# Locations the Businesses-screen test creates through the UI.
E2E_LOCATION_PATTERN = "E2E Location %"


def cmd_login(tenant_id: str) -> None:
    from scripts.seed_dev_users import upsert_login

    from app.users import revoke_access

    db = SessionLocal()
    try:
        user, password = upsert_login(db, *E2E_LOGIN, tenant_ids=(uuid.UUID(tenant_id),))
        # Exactly this location. upsert_login only adds, so a run against
        # another location (E2E_TENANT_ID changed, or CI's pick) left this
        # login with two, and the dashboard opened on the other one, where
        # none of the fixtures are.
        others = db.scalars(
            sqlalchemy.select(TenantMembership.tenant_id).where(
                TenantMembership.user_id == user.id, TenantMembership.tenant_id != uuid.UUID(tenant_id)
            )
        ).all()
        for other in others:
            revoke_access(db, None, user, other)
        db.commit()
        print(json.dumps({"email": user.email, "password": password}))
    finally:
        db.close()


def cmd_login_operator() -> None:
    from scripts.seed_dev_users import upsert_login

    db = SessionLocal()
    try:
        user, password = upsert_login(db, *E2E_OPERATOR, operator=True)
        print(json.dumps({"email": user.email, "password": password}))
    finally:
        db.close()


def cmd_reset_link(tenant_id: str) -> None:
    from scripts.seed_dev_users import upsert_login

    from app.password_reset import issue_token

    db = SessionLocal()
    try:
        user, _ = upsert_login(db, *E2E_RESET_LOGIN, tenant_ids=(uuid.UUID(tenant_id),))
        # Straight to a token: the hourly limit on asking would otherwise
        # stop a fourth run in an hour.
        token = issue_token(db, user)
        db.commit()
        print(json.dumps({"email": user.email, "token": token}))
    finally:
        db.close()


def cmd_cleanup_users() -> None:
    db = SessionLocal()
    try:
        ids = list(db.scalars(sqlalchemy.select(User.id).where(User.email.like(E2E_CREATED_PATTERN))))
        if ids:
            db.execute(
                sqlalchemy.delete(AuditEvent).where(
                    sqlalchemy.or_(AuditEvent.entity_id.in_(ids), AuditEvent.actor_user_id.in_(ids))
                )
            )
            db.execute(sqlalchemy.delete(User).where(User.id.in_(ids)))  # memberships and sessions cascade
            db.commit()
        print(json.dumps({"deleted_users": len(ids)}))
    finally:
        db.close()


def cmd_pick_tenant() -> None:
    db = SessionLocal()
    try:
        # Tied on invoice count (every seeded location has 26), so ties go by
        # name: the ids are random per database, and breaking ties by id
        # made CI test a different location on every run.
        tenant_id = db.scalar(
            sqlalchemy.select(Invoice.tenant_id)
            .join(Tenant, Tenant.id == Invoice.tenant_id)
            .group_by(Invoice.tenant_id, Tenant.name)
            .order_by(sqlalchemy.func.count(Invoice.id).desc(), Tenant.name)
            .limit(1)
            .execution_options(tenant_scope_bypass=True)
        )
        if tenant_id is None:
            sys.exit("no invoices seeded: run the corpus pipeline first")
        print(tenant_id)
    finally:
        db.close()


def cmd_cleanup_locations() -> None:
    db = SessionLocal()
    try:
        ids = list(db.scalars(sqlalchemy.select(Tenant.id).where(Tenant.name.like(E2E_LOCATION_PATTERN))))
        if ids:
            db.execute(sqlalchemy.delete(AuditEvent).where(AuditEvent.tenant_id.in_(ids)))
            db.execute(sqlalchemy.delete(Tenant).where(Tenant.id.in_(ids)))
            db.commit()
        print(json.dumps({"deleted_locations": len(ids)}))
    finally:
        db.close()


def cmd_verify_alias(tenant_id: str, distributor_id: str, raw_sku: str, raw_description: str) -> None:
    """Asks the matcher what THIS tenant now gets for a (distributor, raw_sku).

    Takes a tenant because an alias is no longer globally trusted the instant
    it is written (app/normalize/matcher.py's match_by_alias): the tenant whose
    correction it was gets it immediately, everyone else waits for a second
    independent business to agree. "Whose asking" is the whole question.
    """
    db = SessionLocal()
    result = match_line_item(
        db,
        tenant_id=uuid.UUID(tenant_id),
        distributor_id=uuid.UUID(distributor_id),
        raw_sku=raw_sku,
        raw_description=raw_description,
        raw_pack_size="1 EA",
        quantity=Decimal("1"),
        unit_price=Decimal("5.00"),
        uom="EA",
    )
    print(
        json.dumps(
            {
                "method": result.method,
                "review_status": result.review_status.value,
                "canonical_sku_id": str(result.canonical_sku_id) if result.canonical_sku_id else None,
                "match_confidence": str(result.match_confidence) if result.match_confidence else None,
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    setup_p = sub.add_parser("setup")
    setup_p.add_argument("tenant_id")
    bulk_p = sub.add_parser("setup-bulk")
    bulk_p.add_argument("tenant_id")
    bulk_p.add_argument("count", type=int)
    review_p = sub.add_parser("setup-needs-review")
    review_p.add_argument("tenant_id")

    empty_p = sub.add_parser("setup-empty-invoice")
    empty_p.add_argument("tenant_id")

    login_p = sub.add_parser("login")
    login_p.add_argument("tenant_id")

    sub.add_parser("login-operator")
    sub.add_parser("cleanup-users")
    sub.add_parser("cleanup-locations")
    sub.add_parser("pick-tenant")
    reset_p = sub.add_parser("reset-link")
    reset_p.add_argument("tenant_id")

    cleanup_p = sub.add_parser("cleanup")
    cleanup_p.add_argument("tenant_id")

    verify_p = sub.add_parser("verify-alias")
    verify_p.add_argument("tenant_id")
    verify_p.add_argument("distributor_id")
    verify_p.add_argument("raw_sku")
    verify_p.add_argument("raw_description")

    args = parser.parse_args()
    if args.command == "setup":
        cmd_setup(args.tenant_id)
    elif args.command == "setup-bulk":
        cmd_setup_bulk(args.tenant_id, args.count)
    elif args.command == "setup-needs-review":
        cmd_setup_needs_review(args.tenant_id)
    elif args.command == "setup-empty-invoice":
        cmd_setup_empty_invoice(args.tenant_id)
    elif args.command == "login":
        cmd_login(args.tenant_id)
    elif args.command == "login-operator":
        cmd_login_operator()
    elif args.command == "cleanup-users":
        cmd_cleanup_users()
    elif args.command == "cleanup-locations":
        cmd_cleanup_locations()
    elif args.command == "pick-tenant":
        cmd_pick_tenant()
    elif args.command == "reset-link":
        cmd_reset_link(args.tenant_id)
    elif args.command == "cleanup":
        cmd_cleanup(args.tenant_id)
    elif args.command == "verify-alias":
        cmd_verify_alias(args.tenant_id, args.distributor_id, args.raw_sku, args.raw_description)


if __name__ == "__main__":
    main()
