"""SPEC.md §6: alias lookup -> GTIN match -> pack-size parse -> embedding
similarity, short-circuiting on the first confident hit. Every human
correction writes a sku_aliases row (app/models/sku_alias.py's own docstring:
"This table IS the moat"), which is exactly what makes the alias path free and
compounding — the embedding matcher is the expensive fallback, never the norm
once a distributor's catalog has been seen before.
"""
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from app.models.canonical_sku import CanonicalSku
from app.models.enums import BaseUom, ReviewStatus
from app.models.sku_alias import SkuAlias
from app.models.tenant import Tenant, account_key_column
from app.normalize.description_expansion import description_similarity, normalize_for_embedding
from app.normalize.embeddings import embed_text
from app.normalize.pack_size import ParsedPackSize, PackSizeParseError, parse_pack_size

# Loaded from validation/thresholds.yaml rather than hardcoded literals kept
# in sync by comment: that pattern (which originated here) lets the live
# matcher silently ignore an edit to thresholds.yaml, even though the YAML's
# own header advertises "tightening a threshold is a one-line diff."
# app/analytics/price_creep.py already loads its thresholds this way.
_THRESHOLDS_PATH = Path(__file__).resolve().parents[3] / "validation" / "thresholds.yaml"
_matching_thresholds = yaml.safe_load(_THRESHOLDS_PATH.read_text())["phase3_normalization"]

AUTO_MATCH_CONFIDENCE_THRESHOLD = Decimal(str(_matching_thresholds["auto_match_confidence_threshold"]))
REVIEW_QUEUE_CONFIDENCE_LOW = Decimal(str(_matching_thresholds["review_queue_confidence_low"]))
# How many separate businesses must independently make the same correction
# before it is trusted for everyone else. See match_by_alias.
MIN_INDEPENDENT_ALIAS_CONFIRMATIONS = _matching_thresholds["min_independent_alias_confirmations"]
# How much an incoming description must still look like the one that was
# corrected, before the alias is trusted at all. See match_by_alias.
MIN_ALIAS_DESCRIPTION_SIMILARITY = _matching_thresholds["min_alias_description_similarity"]


@dataclass
class MatchResult:
    canonical_sku_id: uuid.UUID | None
    match_confidence: Decimal | None
    normalized_qty_base: Decimal | None
    normalized_unit_price: Decimal | None
    base_uom: BaseUom | None
    review_status: ReviewStatus
    method: str  # "alias" | "gtin" | "embedding_auto" | "embedding_review" | "new_candidate" | "unparseable_pack_size"


def match_by_alias(
    db: Session,
    distributor_id: uuid.UUID,
    raw_sku: str | None,
    raw_description: str,
    tenant_id: uuid.UUID,
) -> uuid.UUID | None:
    """Exact match on (distributor_id, raw_sku). Free — no embedding call.

    An alias is trusted for this tenant when any of these hold, in order:

    1. Their own business made it. Your correction applies to you immediately —
       keyed on account, not tenant, so a group's other locations count as the
       same business rather than as independent corroboration.
    2. It is system-curated (tenant_id NULL — a catalog import, not one
       person's judgement call).
    3. At least MIN_INDEPENDENT_ALIAS_CONFIRMATIONS *separate businesses* made
       the same mapping.

    Rule 3 is SPEC.md §12's first open question, which had been left as
    "immediately, off one click": a single mistaken correction rewrote matching
    for every other customer, in the table this codebase calls the moat.
    Counting businesses rather than tenants matters for the same reason it does
    in benchmarking — five locations of one group agreeing is one opinion.

    Contradictory mappings that both clear the bar resolve to None rather than
    to the more popular one. Two groups of businesses disagreeing about what a
    distributor's code means is exactly the situation where guessing produces
    a confident false match, and falling through to the embedding path costs
    one similarity search and keeps SPEC.md §6's false-match budget intact.

    Every tier is gated on the description still describing the same item, per
    MIN_ALIAS_DESCRIPTION_SIMILARITY. That is SPEC.md §12's third open
    question — "How do we handle a distributor changing an item code for the
    same product mid-year?" — from its dangerous direction: a distributor who
    REUSES a retired code for a different product turns every alias for it
    into a silent, confident false match, at confidence 1.0, with no embedding
    call to notice and no human ever seeing the line. Checking the description
    each time makes that self-correcting: the drifted line simply stops
    qualifying and resolves on its own merits. Being too strict costs one
    embedding search, which is why the threshold is set where legitimate
    variation never reaches it.
    """
    if not raw_sku:
        return None

    alias_tenant = aliased(Tenant)
    business = account_key_column(alias_tenant)
    # Resolved as a scalar subquery rather than a separate round trip: this
    # runs once per line item, and the worker does ~75 of them per invoice.
    asker_business = select(account_key_column()).where(Tenant.id == tenant_id).scalar_subquery()

    # Individual rows rather than a GROUP BY: the description check below has
    # to run per row, before any counting, or a stale alias would still be
    # corroborating its own reassigned code.
    rows = db.execute(
        select(
            SkuAlias.canonical_sku_id,
            SkuAlias.raw_description,
            SkuAlias.created_at,
            SkuAlias.tenant_id,
            business.label("business"),
            asker_business.label("asker"),
        )
        .select_from(SkuAlias)
        .outerjoin(alias_tenant, alias_tenant.id == SkuAlias.tenant_id)
        .where(SkuAlias.distributor_id == distributor_id, SkuAlias.raw_sku == raw_sku)
    ).all()

    live = [
        row
        for row in rows
        if description_similarity(row.raw_description, raw_description) >= MIN_ALIAS_DESCRIPTION_SIMILARITY
    ]
    if not live:
        return None

    @dataclass
    class _Candidate:
        canonical_sku_id: uuid.UUID
        businesses: set
        curated: bool
        mine: bool
        latest: object

    by_sku: dict[uuid.UUID, _Candidate] = {}
    for row in live:
        candidate = by_sku.get(row.canonical_sku_id)
        if candidate is None:
            candidate = by_sku[row.canonical_sku_id] = _Candidate(row.canonical_sku_id, set(), False, False, row.created_at)
        if row.business is not None:
            candidate.businesses.add(row.business)
        candidate.curated = candidate.curated or row.tenant_id is None
        candidate.mine = candidate.mine or (row.business is not None and row.business == row.asker)
        candidate.latest = max(candidate.latest, row.created_at)

    # Newest first within each tier: a tenant who re-corrects the same code
    # means the later answer, not the earlier one.
    def _newest(candidates):
        return max(candidates, key=lambda c: c.latest).canonical_sku_id

    mine = [c for c in by_sku.values() if c.mine]
    if mine:
        return _newest(mine)

    curated = [c for c in by_sku.values() if c.curated]
    if curated:
        return _newest(curated)

    confirmed = sorted(
        (c for c in by_sku.values() if len(c.businesses) >= MIN_INDEPENDENT_ALIAS_CONFIRMATIONS),
        key=lambda c: len(c.businesses),
        reverse=True,
    )
    if not confirmed:
        return None
    if len(confirmed) > 1 and len(confirmed[0].businesses) == len(confirmed[1].businesses):
        return None
    return confirmed[0].canonical_sku_id


def match_by_gtin(db: Session, gtin: str | None) -> uuid.UUID | None:
    """SPEC.md §6 step 2. Extraction doesn't currently capture a GTIN field
    (SPEC.md §5's contract has no gtin on line items), so `gtin` is always
    None from the current pipeline — this exists so the lookup is correct
    the day a data source for it exists, not as dead code for its own sake.
    """
    if not gtin:
        return None
    return db.scalar(select(CanonicalSku.id).where(CanonicalSku.gtin == gtin))


def match_by_embedding(
    db: Session, raw_description: str, compatible_uoms: set[BaseUom]
) -> tuple[CanonicalSku | None, Decimal | None]:
    """SPEC.md §6 step 4: category/UOM-restricted cosine similarity.

    UOM-restricted via a hard filter (base_uom must be one of compatible_uoms —
    there's no legitimate match across a physical-unit mismatch). Category
    restriction isn't a separate hard filter here: we have no independent
    signal for a raw line's category before it's matched, so it's achieved
    de facto through the embedding itself (semantically distant categories
    score low) plus the UOM filter, rather than a category classifier that
    would just be guessing from the same text the embedding already sees.

    Scored with a single `ORDER BY cosine_distance LIMIT 1` query rather than
    fetching every compatible-UOM candidate and scoring it in Python — this is
    the query shape the HNSW index (app/models/canonical_sku.py) exists to
    accelerate.
    """
    query_text = normalize_for_embedding(raw_description)
    query_vec = embed_text(query_text)

    distance = CanonicalSku.description_embedding.cosine_distance(query_vec)
    row = db.execute(
        select(CanonicalSku, distance.label("distance"))
        .where(
            CanonicalSku.base_uom.in_(compatible_uoms),
            CanonicalSku.description_embedding.is_not(None),
        )
        .order_by(distance)
        .limit(1)
    ).first()
    if row is None:
        return None, None

    candidate, distance_value = row
    similarity = Decimal(str(round(1 - distance_value, 4)))
    return candidate, similarity


def _apply_pack_size(
    pack: ParsedPackSize, quantity: Decimal, unit_price: Decimal, uom: str
) -> tuple[Decimal, Decimal]:
    # unit_price is only a case price when the line is actually billed by the
    # case (printed UOM "CS") — SPEC.md §6 also lists EA/DZ/etc. as UOMs to
    # handle, and a distributor billing directly by the base unit (e.g.
    # UOM="LB") already prices per base unit, so dividing by the pack size
    # again would silently understate normalized_unit_price by that factor.
    if uom.strip().upper() != "CS":
        return quantity, unit_price.quantize(Decimal("0.0001"))
    normalized_qty_base = quantity * pack.base_units_per_case
    normalized_unit_price = (unit_price / pack.base_units_per_case).quantize(Decimal("0.0001"))
    return normalized_qty_base, normalized_unit_price


def _exact_match_result(
    db: Session,
    canonical_sku_id: uuid.UUID,
    method: str,
    raw_pack_size: str,
    quantity: Decimal,
    unit_price: Decimal,
    uom: str,
) -> MatchResult:
    """Shared by the alias and GTIN paths: both are a confirmed exact match on
    identity, differing only in how canonical_sku_id was found — everything
    about normalizing the price is identical from here.
    """
    try:
        pack = parse_pack_size(raw_pack_size)
        qty_base, price_base = _apply_pack_size(pack, quantity, unit_price, uom)
        base_uom = _resolve_base_uom(db, pack, canonical_sku_id)
        review_status = ReviewStatus.auto
    except PackSizeParseError:
        # Identity is still certain (that's what alias/GTIN means), but with no
        # normalized price this row isn't actually fully resolved — auto would
        # claim "no review needed" over a line with no usable price.
        qty_base = price_base = base_uom = None
        review_status = ReviewStatus.pending
    return MatchResult(
        canonical_sku_id=canonical_sku_id,
        match_confidence=Decimal("1.0"),
        normalized_qty_base=qty_base,
        normalized_unit_price=price_base,
        base_uom=base_uom,
        review_status=review_status,
        method=method,
    )


def match_line_item(
    db: Session,
    *,
    distributor_id: uuid.UUID,
    raw_sku: str | None,
    raw_description: str,
    raw_pack_size: str,
    quantity: Decimal,
    unit_price: Decimal,
    uom: str,
    tenant_id: uuid.UUID,
    gtin: str | None = None,
) -> MatchResult:
    alias_match = match_by_alias(db, distributor_id, raw_sku, raw_description, tenant_id)
    if alias_match is not None:
        return _exact_match_result(db, alias_match, "alias", raw_pack_size, quantity, unit_price, uom)

    gtin_match = match_by_gtin(db, gtin)
    if gtin_match is not None:
        return _exact_match_result(db, gtin_match, "gtin", raw_pack_size, quantity, unit_price, uom)

    try:
        pack = parse_pack_size(raw_pack_size)
    except PackSizeParseError:
        # SPEC.md §6: a pack-size error produces a confidently wrong benchmark,
        # worse than no benchmark — never guess forward from here. No embedding
        # search either: without a resolved base UOM there's nothing valid to
        # restrict candidates to.
        return MatchResult(
            canonical_sku_id=None,
            match_confidence=None,
            normalized_qty_base=None,
            normalized_unit_price=None,
            base_uom=None,
            review_status=ReviewStatus.pending,
            method="unparseable_pack_size",
        )

    qty_base, price_base = _apply_pack_size(pack, quantity, unit_price, uom)
    candidate, similarity = match_by_embedding(db, raw_description, pack.compatible_base_uoms)
    # A singleton compatible-UOM set (everything but the "oz" weight/fluid
    # ambiguity) is already unambiguous from the pack string alone; only the
    # ambiguous case needs the matched candidate's own declared base_uom.
    candidate_base_uom = (
        next(iter(pack.compatible_base_uoms)) if len(pack.compatible_base_uoms) == 1 else (candidate.base_uom if candidate else None)
    )

    if candidate is not None and similarity is not None and similarity >= AUTO_MATCH_CONFIDENCE_THRESHOLD:
        return MatchResult(
            canonical_sku_id=candidate.id,
            match_confidence=similarity,
            normalized_qty_base=qty_base,
            normalized_unit_price=price_base,
            base_uom=candidate_base_uom,
            review_status=ReviewStatus.auto,
            method="embedding_auto",
        )
    if candidate is not None and similarity is not None and similarity >= REVIEW_QUEUE_CONFIDENCE_LOW:
        return MatchResult(
            canonical_sku_id=candidate.id,
            match_confidence=similarity,
            normalized_qty_base=qty_base,
            normalized_unit_price=price_base,
            base_uom=candidate_base_uom,
            review_status=ReviewStatus.pending,
            method="embedding_review",
        )

    # Below 0.80, or no candidates at all in this UOM band: SPEC.md §6 — "below
    # 0.80 creates a new canonical SKU candidate." We don't auto-create the row
    # (that's a human call in the Phase 5 review queue); we flag it as pending
    # with no canonical_sku_id so it surfaces there instead of silently
    # attaching to the nearest-but-wrong existing SKU. base_uom still reflects
    # the low-confidence candidate as a hint where the pack string alone was
    # ambiguous — not persisted as an authoritative match either way.
    return MatchResult(
        canonical_sku_id=None,
        match_confidence=similarity,
        normalized_qty_base=qty_base,
        normalized_unit_price=price_base,
        base_uom=candidate_base_uom,
        review_status=ReviewStatus.pending,
        method="new_candidate",
    )


def _resolve_base_uom(db: Session, pack: ParsedPackSize, canonical_sku_id: uuid.UUID) -> BaseUom:
    # CanonicalSku isn't TenantScoped (it's a shared reference table), so this
    # is a plain lookup — no tenant-scope guard to satisfy here.
    if len(pack.compatible_base_uoms) == 1:
        return next(iter(pack.compatible_base_uoms))
    matched = db.get(CanonicalSku, canonical_sku_id)
    return matched.base_uom
