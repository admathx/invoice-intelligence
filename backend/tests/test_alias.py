"""Phase 3b gate, per SPEC.md §10: a confirmed alias short-circuits the
expensive path; verify the embedding matcher is never called on a known alias.
"""
import uuid
from decimal import Decimal

import pytest

from app.db import SessionLocal
from app.models import CanonicalSku, Distributor, SkuAlias
from app.models.enums import BaseUom
from app.normalize.matcher import match_by_gtin, match_line_item


@pytest.fixture()
def db_session():
    session = SessionLocal()
    yield session
    session.close()


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
    sku = CanonicalSku(name=f"Test SKU {uuid.uuid4().hex[:8]}", category="proteins", base_uom=BaseUom.lb)
    db_session.add(sku)
    db_session.commit()
    db_session.refresh(sku)
    return sku


@pytest.fixture()
def confirmed_alias(db_session, distributor, canonical_sku):
    alias = SkuAlias(
        canonical_sku_id=canonical_sku.id,
        distributor_id=distributor.id,
        raw_description="whatever was on the invoice",
        raw_sku="RAW-SKU-123",
    )
    db_session.add(alias)
    db_session.commit()
    return alias


def test_confirmed_alias_short_circuits(db_session, distributor, canonical_sku, confirmed_alias):
    result = match_line_item(
        db_session,
        distributor_id=distributor.id,
        raw_sku="RAW-SKU-123",
        raw_description="SOME GARBLED TEXT THAT WOULD NEVER EMBED-MATCH",
        raw_pack_size="4/5 LB",
        quantity=Decimal("2"),
        unit_price=Decimal("47.50"),
        uom="CS",
    )
    assert result.canonical_sku_id == canonical_sku.id
    assert result.method == "alias"
    assert result.match_confidence == Decimal("1.0")


def test_embedding_matcher_never_called_on_known_alias(monkeypatch, db_session, distributor, canonical_sku, confirmed_alias):
    calls = []
    monkeypatch.setattr(
        "app.normalize.matcher.match_by_embedding",
        lambda *a, **k: (calls.append(1), (None, None))[1],
    )

    match_line_item(
        db_session,
        distributor_id=distributor.id,
        raw_sku="RAW-SKU-123",
        raw_description="irrelevant",
        raw_pack_size="4/5 LB",
        quantity=Decimal("1"),
        unit_price=Decimal("10.00"),
        uom="CS",
    )

    assert calls == [], "embedding matcher was called despite a confirmed alias existing"


def test_alias_is_scoped_to_distributor(db_session, distributor, other_distributor, canonical_sku, confirmed_alias):
    # Same raw_sku, different distributor — must NOT match (SPEC.md §6: exact
    # match on (distributor_id, raw_sku), not raw_sku alone).
    result = match_line_item(
        db_session,
        distributor_id=other_distributor.id,
        raw_sku="RAW-SKU-123",
        raw_description="TOTALLY UNRELATED TEXT THAT WONT EMBED MATCH ANYTHING WELL",
        raw_pack_size="4/5 LB",
        quantity=Decimal("1"),
        unit_price=Decimal("10.00"),
        uom="CS",
    )
    assert result.method != "alias"


def test_no_alias_falls_through_to_embedding(db_session, distributor):
    result = match_line_item(
        db_session,
        distributor_id=distributor.id,
        raw_sku="NEVER-SEEN-BEFORE",
        raw_description="MOZZ SHRD WHL MLK",
        raw_pack_size="4/5 LB",
        quantity=Decimal("1"),
        unit_price=Decimal("47.50"),
        uom="CS",
    )
    assert result.method in ("embedding_auto", "embedding_review", "new_candidate")


def test_gtin_lookup_returns_none_without_gtin_data():
    # SPEC.md §5's extraction contract has no GTIN field — this documents that
    # match_by_gtin is correct-but-inert until a data source for it exists.
    db = SessionLocal()
    try:
        assert match_by_gtin(db, None) is None
        assert match_by_gtin(db, "") is None
    finally:
        db.close()
