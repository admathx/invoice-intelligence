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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.canonical_sku import CanonicalSku
from app.models.enums import BaseUom, ReviewStatus
from app.models.sku_alias import SkuAlias
from app.normalize.description_expansion import normalize_for_embedding
from app.normalize.embeddings import embed_text
from app.normalize.pack_size import ParsedPackSize, PackSizeParseError, parse_pack_size

# Mirrors validation/thresholds.yaml's phase3_normalization block.
AUTO_MATCH_CONFIDENCE_THRESHOLD = Decimal("0.92")
REVIEW_QUEUE_CONFIDENCE_LOW = Decimal("0.80")


@dataclass
class MatchResult:
    canonical_sku_id: uuid.UUID | None
    match_confidence: Decimal | None
    normalized_qty_base: Decimal | None
    normalized_unit_price: Decimal | None
    base_uom: BaseUom | None
    review_status: ReviewStatus
    method: str  # "alias" | "gtin" | "embedding_auto" | "embedding_review" | "new_candidate" | "unparseable_pack_size"


def match_by_alias(db: Session, distributor_id: uuid.UUID, raw_sku: str | None) -> uuid.UUID | None:
    """Exact match on (distributor_id, raw_sku). Free — no embedding call."""
    if not raw_sku:
        return None
    return db.scalar(
        select(SkuAlias.canonical_sku_id).where(
            SkuAlias.distributor_id == distributor_id, SkuAlias.raw_sku == raw_sku
        )
    )


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
    gtin: str | None = None,
) -> MatchResult:
    alias_match = match_by_alias(db, distributor_id, raw_sku)
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
