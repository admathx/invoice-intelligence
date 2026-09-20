"""Phase 3b gate, per SPEC.md §10: a confirmed alias short-circuits the
expensive path; verify the embedding matcher is never called on a known alias.

Also covers SPEC.md §12's first open question — "Should a corrected alias
apply across all tenants immediately, or only after N confirmations?" — which
is resolved here as the spec's own leaning said: your correction is yours
immediately, and it reaches everyone else once a second independent business
agrees.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db import SessionLocal
from app.models import Account, CanonicalSku, Distributor, SkuAlias, Tenant
from app.models.enums import BaseUom, VolumeTier
from app.normalize.matcher import MIN_INDEPENDENT_ALIAS_CONFIRMATIONS, match_by_gtin, match_line_item

RAW_SKU = "RAW-SKU-123"

# db_session comes from conftest.py, which rolls back everything a test
# commits. This module used to define its own committing fixture, which is
# where the stray sku_aliases rows in the dev database came from.


@pytest.fixture()
def distributor(db_session):
    d = Distributor(name="Test Distributor", slug=f"test-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


@pytest.fixture()
def other_distributor(db_session):
    d = Distributor(name="Other Distributor", slug=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


@pytest.fixture()
def canonical_sku(db_session):
    return _make_sku(db_session, "Test SKU")


def _make_sku(db, name: str) -> CanonicalSku:
    sku = CanonicalSku(name=f"{name} {uuid.uuid4().hex[:8]}", category="proteins", base_uom=BaseUom.lb)
    db.add(sku)
    db.commit()
    db.refresh(sku)
    return sku


def _make_account(db) -> Account:
    account = Account(name=f"Alias Group {uuid.uuid4().hex[:8]}")
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def _make_tenant(db, account: Account | None = None) -> Tenant:
    tenant = Tenant(
        name=f"Alias Test Tenant {uuid.uuid4().hex[:8]}",
        metro="alias-test-metro",
        volume_tier=VolumeTier.under_500k,
        account_id=account.id if account is not None else None,
    )
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


def _write_alias(db, distributor, sku, tenant: Tenant | None, *, created_at: datetime | None = None) -> SkuAlias:
    """A correction by `tenant`, or a system-curated alias when tenant is None."""
    alias = SkuAlias(
        canonical_sku_id=sku.id,
        distributor_id=distributor.id,
        tenant_id=tenant.id if tenant is not None else None,
        raw_description="whatever was on the invoice",
        raw_sku=RAW_SKU,
        # Passed explicitly where ordering is under test: Postgres' now() is
        # the TRANSACTION timestamp, so rows written inside one test would
        # otherwise share a created_at and tie.
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db.add(alias)
    db.commit()
    return alias


def _match(db, distributor, tenant: Tenant, raw_description: str = "SOME GARBLED TEXT THAT WOULD NEVER EMBED-MATCH"):
    return match_line_item(
        db,
        tenant_id=tenant.id,
        distributor_id=distributor.id,
        raw_sku=RAW_SKU,
        raw_description=raw_description,
        raw_pack_size="4/5 LB",
        quantity=Decimal("2"),
        unit_price=Decimal("47.50"),
        uom="CS",
    )


@pytest.fixture()
def curated_alias(db_session, distributor, canonical_sku):
    return _write_alias(db_session, distributor, canonical_sku, None)


# --- the original Phase 3b gate --------------------------------------------


def test_confirmed_alias_short_circuits(db_session, distributor, canonical_sku, curated_alias):
    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.canonical_sku_id == canonical_sku.id
    assert result.method == "alias"
    assert result.match_confidence == Decimal("1.0")


def test_embedding_matcher_never_called_on_known_alias(
    monkeypatch, db_session, distributor, canonical_sku, curated_alias
):
    calls = []
    monkeypatch.setattr(
        "app.normalize.matcher.match_by_embedding",
        lambda *a, **k: (calls.append(1), (None, None))[1],
    )

    _match(db_session, distributor, _make_tenant(db_session), raw_description="irrelevant")

    assert calls == [], "embedding matcher was called despite a confirmed alias existing"


def test_alias_is_scoped_to_distributor(db_session, distributor, other_distributor, canonical_sku, curated_alias):
    # Same raw_sku, different distributor — must NOT match (SPEC.md §6: exact
    # match on (distributor_id, raw_sku), not raw_sku alone).
    result = _match(
        db_session,
        other_distributor,
        _make_tenant(db_session),
        raw_description="TOTALLY UNRELATED TEXT THAT WONT EMBED MATCH ANYTHING WELL",
    )
    assert result.method != "alias"


def test_no_alias_falls_through_to_embedding(db_session, distributor):
    result = match_line_item(
        db_session,
        tenant_id=_make_tenant(db_session).id,
        distributor_id=distributor.id,
        raw_sku="NEVER-SEEN-BEFORE",
        raw_description="MOZZ SHRD WHL MLK",
        raw_pack_size="4/5 LB",
        quantity=Decimal("1"),
        unit_price=Decimal("47.50"),
        uom="CS",
    )
    assert result.method in ("embedding_auto", "embedding_review", "new_candidate")


def test_gtin_lookup_returns_none_without_gtin_data(db_session):
    # SPEC.md §5's extraction contract has no GTIN field — this documents that
    # match_by_gtin is correct-but-inert until a data source for it exists.
    assert match_by_gtin(db_session, None) is None
    assert match_by_gtin(db_session, "") is None


# --- SPEC.md §12 Q1: how far does one correction travel? -------------------


def test_your_own_correction_applies_to_you_immediately(db_session, distributor, canonical_sku):
    corrector = _make_tenant(db_session)
    _write_alias(db_session, distributor, canonical_sku, corrector)

    result = _match(db_session, distributor, corrector)

    assert result.method == "alias"
    assert result.canonical_sku_id == canonical_sku.id


def test_one_businesss_correction_does_not_reach_another(db_session, distributor, canonical_sku):
    """The bug this resolves: a single person's click silently rewrote
    matching for every other customer of the product.
    """
    _write_alias(db_session, distributor, canonical_sku, _make_tenant(db_session))

    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method != "alias"


def test_a_second_independent_business_promotes_the_alias(db_session, distributor, canonical_sku):
    for _ in range(MIN_INDEPENDENT_ALIAS_CONFIRMATIONS):
        _write_alias(db_session, distributor, canonical_sku, _make_tenant(db_session))

    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method == "alias"
    assert result.canonical_sku_id == canonical_sku.id


def test_two_locations_of_one_group_are_not_two_confirmations(db_session, distributor, canonical_sku):
    """Five locations of one chain agreeing is one opinion — the same
    distinction benchmark suppression makes.
    """
    group = _make_account(db_session)
    for _ in range(MIN_INDEPENDENT_ALIAS_CONFIRMATIONS + 2):
        _write_alias(db_session, distributor, canonical_sku, _make_tenant(db_session, account=group))

    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method != "alias"


def test_a_sibling_location_gets_its_own_groups_correction_immediately(db_session, distributor, canonical_sku):
    group = _make_account(db_session)
    _write_alias(db_session, distributor, canonical_sku, _make_tenant(db_session, account=group))

    result = _match(db_session, distributor, _make_tenant(db_session, account=group))

    assert result.method == "alias"
    assert result.canonical_sku_id == canonical_sku.id


def test_contradictory_confirmed_mappings_fall_through_rather_than_guess(db_session, distributor):
    """Two camps of businesses disagreeing about what a code means is exactly
    where guessing produces a confident false match. Cost of falling through
    is one embedding search.
    """
    one, other = _make_sku(db_session, "Claimed A"), _make_sku(db_session, "Claimed B")
    for sku in (one, other):
        for _ in range(MIN_INDEPENDENT_ALIAS_CONFIRMATIONS):
            _write_alias(db_session, distributor, sku, _make_tenant(db_session))

    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method != "alias"


def test_a_clear_majority_beats_a_confirmed_minority(db_session, distributor):
    popular, fringe = _make_sku(db_session, "Popular"), _make_sku(db_session, "Fringe")
    for _ in range(MIN_INDEPENDENT_ALIAS_CONFIRMATIONS + 1):
        _write_alias(db_session, distributor, popular, _make_tenant(db_session))
    for _ in range(MIN_INDEPENDENT_ALIAS_CONFIRMATIONS):
        _write_alias(db_session, distributor, fringe, _make_tenant(db_session))

    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method == "alias"
    assert result.canonical_sku_id == popular.id


def test_a_curated_alias_needs_no_confirmation(db_session, distributor, canonical_sku, curated_alias):
    """A catalog import isn't one person's judgement call, so it doesn't wait
    for a second opinion.
    """
    result = _match(db_session, distributor, _make_tenant(db_session))

    assert result.method == "alias"
    assert result.canonical_sku_id == canonical_sku.id


def test_a_later_correction_supersedes_the_same_tenants_earlier_one(db_session, distributor):
    """SPEC.md §12's third open question, from the one angle this table can
    answer: a distributor reassigning an item code mid-year means the tenant's
    newer correction is the true one, not the older.
    """
    was, now = _make_sku(db_session, "Discontinued"), _make_sku(db_session, "Replacement")
    corrector = _make_tenant(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _write_alias(db_session, distributor, was, corrector, created_at=old)
    _write_alias(db_session, distributor, now, corrector, created_at=old + timedelta(days=29))

    result = _match(db_session, distributor, corrector)

    assert result.canonical_sku_id == now.id
