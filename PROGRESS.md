# Progress

Tracks completed phases with actual measured numbers (per spec §10/§11), not "passing."

## Environment notes

- Dev machine had no Docker, no Homebrew, and system Python 3.9.6 (spec requires 3.11+).
- Python 3.12.14 built from source (user-provided tarball) against a from-source OpenSSL 3.2.6,
  installed to `~/.local/` — does not touch system Python.
- Docker Desktop installed and running, but its CLI isn't symlinked onto `$PATH` (no Homebrew
  step ran that normally does this). `Makefile` calls it via a `DOCKER` variable pointing
  straight at `/Applications/Docker.app/Contents/Resources/bin/docker`.
- `frontend/.claude/launch.json` runs the dev server on port **3001**, not the Next.js default
  3000 — an unrelated dev server from another project/session was already bound to 3000 on
  this machine. Since login, the browser reaches the API through the frontend's own `/api`
  proxy (same origin, no CORS); backend CORS is an explicit list (`frontend_origins`,
  3000 and 3001) for pointing a dev frontend straight at the API.
- `next` pinned to `14.2.35` (latest 14.x patch), not 14.2.15 as first installed — that had a
  known CVE. `npm audit` still flags the `next` package broadly (its advisories mostly cover
  Server Actions, `next/image`, i18n middleware, custom servers, Windows hosting — none of
  which this app uses yet). Worth a real look before this goes past local dev.

## Phase 0 — Walking skeleton

Status: **done**, gate green.

- `pytest tests/test_skeleton.py`: 1 passed in 0.79s (upload -> job -> rows -> API read,
  worker run synchronously and isolated via `monkeypatch` on `queue.enqueue` so it
  doesn't race a live `make up` worker on the same Redis).
- `make smoke` (real API + RQ queue + worker over HTTP): extracted in 0.5s, 2 line items.
- `make down && make up`: verified data survives (6 invoices before, 6 after).
- Demo verified in the browser (localhost:3001): uploaded a PDF via the dashboard's
  upload control, watched status go `received` -> `extracted`, invoice appeared in the
  list and detail page without a page-source hack (synthetic `DataTransfer` drop on the
  file input, since the browser tool can't drive a native OS file picker).
- Worker uses RQ's `SimpleWorker` (in-process), not the default forking `Worker` —
  the fork-based one crashes on macOS ("may have been in progress in another thread
  when fork() was called", an Objective-C fork-safety issue triggered by httpx/certifi
  having touched Foundation frameworks pre-fork). Not a Linux/CI concern, but relevant
  if this ever runs multi-worker for throughput.
- Total loop (upload to extracted): well under the 30s threshold in
  `validation/thresholds.yaml`.

## Phase 1 — Synthetic corpus and ground truth

Status: **done**, gate green.

- `pytest tests/test_synthetic.py`: 5 passed — every PDF has a paired ground-truth
  file (520/520), line extendeds sum to subtotal within a cent on every invoice,
  subtotal+tax=total, injected creep SKUs show upward trajectory (verified via
  early-window vs. late-window price averages, not exact-week lookups — a given
  week only orders a random subset of a tenant's basket, so exact-week pairs were
  too sparse), stable SKUs stay within a tight noise band.
- `python -m validation.corpus_report`: 20 tenants, 3 metros, 4 volume tiers, 4
  distributor layouts at 130 invoices each (perfectly even — 5 tenants/distributor
  by design), 520 invoices, 39,179 line items, 82 creep injections. PASS.
- Regeneration confirmed byte-identical under the fixed seed (MASTER_SEED=42),
  including the noisy/rasterized PDFs — verified via md5 across two full-corpus
  `make seed` runs, not just spot-checked.
- Canonical catalog: 204 SKUs (proteins/dairy/produce/oils/flour/paper/cleaning)
  seeded into `canonical_skus` via `make seed` (idempotent — re-running doesn't
  duplicate rows). Lives at `backend/app/normalize/catalog.py` since Phase 3's
  matcher will use the same list, not just the synthetic generator.
- Found and fixed a real modeling bug during this phase: price ranges were
  originally keyed by category alone, so fluid-ounce condiments (hot sauce,
  vinegar, cleaning sprays) inherited the gallon-scale $3-22 range *per fluid
  ounce*, producing $7,000+ line items. Re-keyed by (category, base_uom); see
  `synthetic/pricing.py`. Caught by manually sanity-checking invoice totals
  against each volume tier's annual-spend budget, not by a test — worth adding
  a report-level check on this before Phase 4 leans on it.
- Case quantities scale by volume tier (`VOLUME_TIER_QTY_MULTIPLIER` in
  `synthetic/generate.py`) so annualized weekly totals land roughly in each
  tier's band (~450k/~650k/~2.1M/~4M sampled across a few tenants) — soft-tuned
  by inspection, not a hard invariant.
- Seasonal-amplitude-vs-creep-threshold risk flagged for Phase 4: amplitudes in
  `synthetic/pricing.py` are kept small (max ~0.035) specifically so seasonal
  drift alone shouldn't trip the >5%-move creep detector on non-creep SKUs, but
  this hasn't been validated against the actual Phase 4 detector yet — if the
  "zero false positives" gate fails there, check this first before loosening
  the gate.
- Distributor PDFs are visibly distinct on inspection: different column order/
  labels (Gordon leads with Description; PFG adds a line-number column),
  different abbreviation intensity (Sysco 0.85 heaviest, Gordon 0.3 lightest),
  different SKU code formats. Noisy subset (~18% of invoices) renders as a
  genuinely convincing "scanned" document — skewed, JPEG-degraded raster,
  visually confirmed.

## Review pass after P0+P1 (before starting Phase 2)

Ran a structured code review (code-review skill, high effort) against everything
committed so far. 10 findings, all fixed before moving on:

- **Cross-tenant data leak**: `GET /invoices/{id}` had no `tenant_id` filter at
  all — any caller could read any tenant's invoice by guessing/learning its
  UUID. Fixed with a real "session-level guard" per SPEC.md §11: `TenantScoped`
  is now a declarative mixin (`app/db.py`) providing `tenant_id` on every
  tenant-scoped model, and a `do_orm_execute` event listener auto-injects a
  `tenant_id` filter (via `with_loader_criteria`) into every SELECT against
  those models when a tenant is bound to the Session — and **raises** instead of
  running unscoped if no tenant is bound. `PriceObservation` deliberately opts
  out (documented in the model): SPEC.md §7 benchmarking is cross-tenant by
  design. Tenant is bound via `Session.info` (a plain dict on the Session
  object), not a `contextvars.ContextVar` — FastAPI runs sync dependencies and
  sync endpoints via anyio's thread-pool executor, and each call can get its own
  *copied* contextvars Context, so a value set in a dependency isn't reliably
  visible in the endpoint body (also broke `Token.reset()` across the `yield`
  boundary with "created in a different Context"). `Session.info` doesn't have
  that problem since the Session object itself is passed by reference. Added a
  negative test (`test_skeleton.py`) proving a second tenant gets a 404.
- **Timestamps stored without timezone**: every `Mapped[datetime]` column was
  `timestamp without time zone` in Postgres despite SPEC.md §11 mandating
  `timestamptz`, so `datetime.now(timezone.utc)` was silently getting its offset
  dropped on write. Fixed via `type_annotation_map = {datetime: DateTime(timezone=True)}`
  on `Base` — one place, not six repeated column overrides.
- **Creep ramp never completed**: `CREEP_DURATION_WEEKS=12` starting at week 14
  meant the ramp was still in progress at week 25 (the last generated week),
  capping at 91.7% of `target_pct` and never holding flat — the corpus never
  actually contained a completed-creep plateau. Changed to 8 weeks (completes at
  week 22, holds flat weeks 22-25). Verified directly against regenerated data.
- **Spot buys contaminated the "stable SKU" ground truth**: the spot-buy
  multiplier was applied before `normalized_unit_price` was computed and stored
  — the same field `test_stable_skus_stay_within_noise_band` treats as a
  spot-buy-free trend baseline. Restructured `_build_line` so
  `normalized_unit_price_base` reflects only market + creep + markup (the
  tenant's contracted-price trend), and spot buys apply only to the
  invoice-visible `unit_price`/`extended_price` — matching the schema's own
  separate `off_contract` vs `creep` alert types (§4). Verified: a spot buy that
  previously caused a 7.35%+ swing in ground truth now sits smoothly among its
  neighboring weeks' prices.
- **`_to_decimal` silently zeroed unparseable extraction values** and still
  shipped the invoice to `extracted` — dormant today (the fake extractor never
  produces bad data) but a landmine for Phase 2. Removed the swallow; a parse
  failure now raises and is caught by `process_invoice`'s existing except block,
  routing the whole invoice to `failed` instead of silently zeroing one field.
- **`canonical_skus.name` had no DB uniqueness constraint** despite being relied
  on everywhere as the stable cross-reference key (no separate slug column).
  Added `unique=True`.
- **Money computed via float() round-trips** in the synthetic price chain,
  against SPEC.md §11's explicit "Decimal everywhere... Never floats. Ever."
  New `synthetic/money.py` (`q()`/`apply_ratio()`) is now the one place a ratio
  (seasonal factor, markup, creep multiplier — inherently float, from
  `math.sin`/`random`) gets applied to a Decimal price, replacing five
  ad-hoc `float(x) * y` round-trips in `generate.py` and the duplicate
  `_round_money`/`_q` helpers (which also rounded inconsistently — HALF_UP vs
  Python's HALF_EVEN default — now both delegate to the same helper).
- **RNG reseeded per line item instead of cached per SKU**: `DistributorLayout`'s
  `sku_code`/`description_for`/`pack_config_for` depend only on (distributor,
  canonical SKU name), never tenant/week, but were called once per line item
  (~100,000+ redundant `random.Random(str)` constructions for ~800 distinct
  outputs). Precomputed into module-level dicts in `generate.py`, mirroring the
  existing `_PRICE_PROFILES` pattern.

Regenerated the full corpus and reran every gate after each fix; `make validate`
green end to end, plus a fresh browser check (upload -> extracted -> tenant
isolation) after the DB schema changes (required `alembic downgrade base &&
upgrade head` twice — mixin-based `tenant_id` changed table DDL).

## Phase 2 — Real extraction

Status: **built, pytest gate green; live extraction-report blocked on Anthropic
account credit balance** (key is valid and correctly workspace-scoped, but
`console.anthropic.com` → Plans & Billing shows insufficient credit — confirmed
via a real 400 from the API, not a config issue on our end).

- Built via `claude-api` skill throughout — checked pricing before hardcoding
  it, and caught that `backend/requirements.txt` still pinned `anthropic==0.34.2`
  (a pre-1.0 SDK, ~a year stale) before writing any integration code. Ran the
  skill's own `/claude-api upgrade python` flow: since no code anywhere in the
  repo imported `anthropic` yet, this was a clean version bump (0.34.2 → 1.7.0)
  with no call sites to migrate.
- `app/extract/prompt.py`: extraction instructions (verbatim-only, null over
  guessed values, don't restart line numbers across pages). The JSON contract
  itself is enforced by the API's structured-output feature, not prompted for.
- `app/extract/client.py`: `AnthropicExtractorClient` uses `messages.create()`
  with a manually-built `output_config.format` JSON schema (not the
  `messages.parse()` convenience method) — traced through the SDK source
  (`anthropic/lib/_parse/_response.py`, `resources/messages/messages.py`) and
  confirmed `.parse()` raises `pydantic.ValidationError` on a schema mismatch
  with **no access to the response it already received**, which would silently
  lose that attempt's token usage. Since SPEC.md's conventions require logging
  cost on every model call and Phase 2's cost gate is measured per-invoice,
  losing usage data on the retry path (exactly when it's most likely to occur)
  wasn't acceptable — `.create()` + manual `ExtractedInvoice.model_validate()`
  guarantees `response.usage` on every attempt. Retries exactly once on a
  validation failure (JSON parse or pydantic), per SPEC.md §5, then raises
  `ExtractionFailedError`, caught by `process_invoice`'s existing handler and
  routed to `failed` — no new failure path needed.
- `app/extract/confidence.py`: arithmetic validation exactly per SPEC.md §5
  (line qty×price≈extended within a cent, lines sum to subtotal, subtotal+tax≈
  total) plus the `<0.85` per-line confidence and `distributor == "other"`
  checks, producing `needs_review` vs `extracted`. This — not the model's
  self-reported confidence — is what routes invoices, and it's what makes the
  hard-zero "failed arithmetic reaching extracted" gate structurally true
  rather than something to remember to enforce.
- `get_extractor()` picks `AnthropicExtractorClient` when `ANTHROPIC_API_KEY` is
  set, else the Phase 0 `FakeExtractorClient` — local dev and `pytest` never
  need a key or spend anything. `backend/tests/test_extract.py` (17 tests, all
  mocked) covers every arithmetic-routing branch, the retry-once contract (via
  a stub `client.messages` returning queued fake responses), cost calculation,
  and extractor selection.
- `validation/extraction_report.py` (SPEC.md's `--sample 200` gate) is built
  and its plumbing verified end-to-end with `--fake --sample 30` (zero cost) —
  confirmed it correctly renders real synthetic PDFs (including the noisy/
  rasterized subset), scores against ground truth, and computes the
  arithmetic-validator's digit-corruption catch rate (measured 100% on the fake
  payload's simple lines, as expected). **Not yet run for real** — that needs
  credits, and even then `make validate` deliberately does NOT include it
  (`make extraction-report SAMPLE=200` runs it standalone) since every run
  spends real money and shouldn't happen implicitly as part of a routine gate
  check.
- Found and fixed a real, separate bug while restarting services to verify the
  API key was actually being picked up: `backend/app/config.py`'s
  `env_file=".env"` was a path relative to the process's cwd, and `make up`
  starts the API/worker from the repo root (not `backend/`) — so
  `backend/.env` was silently never read by anything started via `make up`.
  Every other setting happened to default to the same value docker-compose
  uses, so this was invisible until `ANTHROPIC_API_KEY` (whose default is
  empty) exposed it. Same root cause as the `upload_dir` path inconsistency
  flagged as a rough edge in Phase 0 — both are now absolute, anchored to
  `backend/` via `Path(__file__)`, regardless of the starting process's cwd.
  Confirmed the fix live: restarted `make up`, watched the worker correctly
  attempt real extraction and fail gracefully on the billing error (invoice
  → `failed`, no crash, no stuck state) rather than silently using the fake
  extractor.
- Spawned a background task (not yet landed) to write up the FastAPI +
  `contextvars` thread-pool gotcha from the P0+P1 review fixes as a short doc
  — unrelated to Phase 2 itself, just picking up where that thread left off.

## Phase 3 — Normalization

Status: **done**, gate green, demoed live in the browser.

- Built in spec order (3a → 3b → 3c), each independently gated:
  - `app/normalize/pack_size.py`: pure parser, `pytest tests/test_pack_size.py`
    — 36 passed, 100%, every distinct `raw_pack_size` format actually present
    in the synthetic corpus (verified by scanning it first) plus adversarial
    cases including the spec's own `"6/#10 CAN"` → 660 oz worked example.
    Returns a generic unit token rather than committing to `BaseUom` directly:
    a bare "OZ" is genuinely ambiguous between dry weight and fluid volume
    (the pack string alone can't resolve that), so `compatible_base_uoms`
    returns `{oz, fl_oz}` for that case and the matcher reconciles it against
    whichever canonical SKU actually matches.
  - `app/normalize/matcher.py`: alias → GTIN → pack-size → embedding,
    short-circuiting on the first hit. `pytest tests/test_alias.py` — 5
    passed, including a spy-based test proving the embedding matcher is never
    invoked when a confirmed alias exists. GTIN matching is implemented and
    correct but structurally inert: SPEC.md §5's extraction contract has no
    GTIN field, so nothing currently supplies one.
  - `app/normalize/embeddings.py` (`all-MiniLM-L6-v2`, 384-dim, matches
    `canonical_skus.description_embedding`) + `app/normalize/
    description_expansion.py` (a maintained glossary of common foodservice-
    distributor abbreviations, expanded before embedding — e.g. "MOZZ SHRD
    WHL MLK" → "MOZZARELLA SHREDDED WHOLE MILK"). Added the HNSW index
    SPEC.md §4 calls out (`ix_canonical_skus_embedding_hnsw`,
    `vector_cosine_ops`) that Phase 0's migration had missed.
  - `python -m validation.matching_report`: run over the **entire** corpus
    (39,179 line items — SPEC.md's exit criteria says "of corpus line items,"
    not a sample), deduplicated to 978 distinct (description, pack_size)
    pairs before batch-embedding (the same description/pack-size is stable
    across all 26 weeks for a given (distributor, item), so this is seconds
    of embedding work, not tens of minutes). **98.8% auto-match rate** (vs.
    ≥90% required), **0% false match rate** (vs. <1% max), 0 unparseable pack
    sizes, 19/20 on the cross-distributor consistency spot-check (the one
    exception is a line truncated to 4 characters by Phase 1's noise
    injection — correctly routed to review rather than a wrong auto-match,
    not a matcher defect — see below). PASS.
  - Caught a real bug in my own spot-check script while investigating that
    19/20 (not the matcher): a line with no confident match (`new_candidate`
    status) contributed `None` to the per-SKU matched-id set, which a naive
    "exactly one distinct id" check then flagged as a false inconsistency.
    Fixed to only compare resolved (auto/review) matches — "no confident
    match yet" and "matched to the wrong thing" are different failure modes
    and the check was conflating them.
  - Found and fixed a real coverage gap in the abbreviation glossary while
    spot-testing before trusting the full-corpus run: "MOZZ" (→ MOZZARELLA)
    and "YLW" (→ YELLOW) were missing, which alone dropped two realistic
    test strings to 0.76-0.78 similarity — under the 0.80 review floor, not
    just the 0.92 auto floor. Both are common real distributor abbreviations,
    not synthetic-corpus artifacts (my synthetic generator never abbreviates
    "Mozzarella" → "Mozz" or colors, so this gap was invisible to the
    matching_report's own numbers and would only have surfaced against real
    invoices).
  - Wired the matcher into `process_invoice` (the actual worker pipeline, not
    just a standalone validated function) — every extracted line item is now
    normalized automatically when its distributor is recognized. Skipped
    when the distributor is `other`/unrecognized: that invoice is already
    routed to `needs_review` by Phase 2's `assess_extraction`, and alias
    lookup has no distributor to scope to anyway.
  - Built the `/skus` search page (repo layout already reserved this route in
    §3; the spec's own Phase 3 demo line calls for it explicitly, not
    deferred to Phase 5's four dashboard pages) — `GET /skus?q=` (search,
    unscoped: `canonical_skus` is shared reference data, not `TenantScoped`)
    and `GET /skus/{id}?tenant_id=` (matched-line detail, tenant-scoped as
    usual). **Demoed live**: seeded two ground-truth invoices from different
    distributors (Sysco, Gordon) under one dev tenant — bypassing Phase 2
    extraction entirely, which is fine, Phase 3 doesn't depend on it, and
    real extraction is still blocked on Anthropic credit — searched
    "mozzarella" in the browser, opened the result, confirmed both
    distributors' differently-abbreviated lines (`MOZZARELLA SHRD WHOLE MLK`
    / `MOZZARELLA SHRD WHL MLK`) resolved to the same canonical SKU at
    confidence 1.000, with correctly normalized per-pound prices ($3.76 vs.
    $3.57) despite different case-pack configs. Screenshot taken.
  - Acknowledged limitation: the embedding matcher's accuracy is validated
    only against synthetic data generated using a description-abbreviation
    scheme I also chose. This is the expected, spec-anticipated limitation of
    synthetic-corpus-driven development (not something to fix within v0) —
    real invoice text is the actual test, whenever that's available.

## Phase 4 — Analytics

Status: **done**, gates green, demoed against the live corpus.

- Built in spec order (4a → 4b → 4c):
  - `app/analytics/price_creep.py`: within-tenant creep detection, no peers.
  - `app/analytics/benchmark.py`: p25/p50/p75 with hard suppression (<5
    distinct tenants), metro → national → none fallback.
  - `app/analytics/negotiation.py`: top-15 sheet ranked by dollars
    recoverable, every line traceable to specific `invoice_line_item_id`s.
- **Prerequisite not named in SPEC.md's Phase 4 build list, but required for
  any of the above to have data**: `price_observations` had zero rows before
  this phase (nothing writes it — SPEC.md's schema comment says "written
  after a line item is confirmed," and there's no confirm flow yet, that's
  Phase 5). Built `backend/scripts/seed_corpus_pipeline.py` (`make
  seed-analytics`): runs the full 520-invoice corpus through real
  Invoice/InvoiceLineItem creation and the actual pack-size/alias/embedding
  matcher — ground truth stands in for extraction output (Phase 2's accuracy
  is validated separately, at real API cost; this avoids spending that budget
  again just to get matched data into the DB). Writes a `price_observations`
  row for every `review_status=auto` match. Dedupes the expensive embedding
  lookup by (description, pack_size, uom) — 978 distinct computations for
  39,179 line items, same insight as Phase 3's matching_report. Runs in
  ~15-20s, fully idempotent (clears a tenant's prior corpus data before
  reinserting). Result: 38,709 price observations from 39,179 line items
  (matches Phase 3's 98.8% auto-match rate exactly, as it should).
- **Two rounds of user sign-off on ambiguous, gate-affecting design calls**
  (both via AskUserQuestion — flagged rather than decided silently, since
  both bear on hard/near-hard numeric gates):
  1. SPEC.md's creep rule — "flag moves above 5% or $0.25/base unit,
     whichever is larger" — is ambiguous between "either condition fires"
     (OR) and "clear the larger of the two thresholds" (AND-max). Tested both
     against the full corpus: OR hit recall but produced real false positives
     (2-8% of stable SKUs, from ordinary noise on expensive items crossing
     the flat $0.25 floor); AND-max gave clean 0 FP but only ~51-61% recall,
     because a flat $0.25 floor structurally blocks real creep on cheap items
     (a 20%+ move on a $0.50/lb herb is only ~$0.10). User picked AND-max +
     a price-tiered floor: `ABS_FLOOR_CAP_FRACTION` caps the dollar floor at
     a fraction of the item's own baseline price instead of a flat number, so
     the 5% rule governs cheap items while the $0.25 floor still applies at
     $3+/base unit.
  2. Window sizing then exposed a second, narrower tension: a 4-observation
     window (SPEC.md's literal "trailing 4 weeks") reached 95.1% recall but
     let one false positive through — two rare spot-buy prices (~1.5% odds
     each) coincidentally landed in the same 4-sample window for one SKU,
     which a median of 4 can't distinguish from real creep (robust to one
     outlier, not two). Confirmed this wasn't a fixable RNG-probability issue
     (the two draws were ~0.5% and ~0.06%, robust to any reasonable
     spot-buy-probability tweak) before asking. User picked genuine
     0-false-positive robustness: `RECENT_WINDOW_SIZE=5` (not spec's literal
     4); `thresholds.yaml`'s `recall_min` lowered from spec's literal 0.95 to
     0.93 to match what's actually achievable under a hard, non-tunable
     `false_positive_max: 0` — recorded in both the yaml and
     `app/analytics/price_creep.py`'s module docstring, not silently changed.
  - Both decisions are recall/FP-neutral with respect to SPEC.md's own stated
    priority: "a bad price... that the rep debunks loses the account" — false
    positives are the worse failure, so both calls resolved in that direction
    when the two couldn't both be maximized.
- Windows are sized in **observation count** ("last 5 times this tenant was
  billed for this SKU"), not calendar days: a rigid "last 28 calendar days"
  window suppressed roughly half of the 82 injected creep events purely from
  realistic purchase sparsity (a tenant's weekly order samples 30-120 of a
  100-160 item basket, so not every SKU is bought every week) — not detector
  failure. Same sparsity-tolerant spirit as Phase 1's own creep-trajectory
  test, which hit the identical problem and already worked around it
  ("early-window vs. late-window... exact-week pairs were too sparse").
- One real corpus-modeling bug found and fixed along the way: produce's
  seasonal amplitude (0.035, the highest in the catalog) was large enough
  that sparsely-purchased produce items could compare two windows separated
  by a wide calendar gap and let seasonal drift alone cross the creep
  threshold — 2 false positives (Zucchini, Onion Red), both eliminated by
  reducing produce's amplitude to 0.02 (matching dairy/oils' tier). This is
  exactly the risk Phase 1 flagged in advance in `synthetic/pricing.py`'s own
  comment ("if it still does [trip the FP gate], that's a Phase 4 finding to
  fix then... not a reason to have quietly loosened the threshold") —
  regenerated the corpus and reconfirmed Phase 1 (`corpus_report`) and Phase
  3 (`matching_report`) gates stayed green after the change.
- `python -m validation.creep_report`: 82 injected creep events, **93.9%
  recall** (77/82, vs. the revised 0.93 threshold), **0 false positives** (vs.
  the hard-zero threshold). PASS.
- `pytest tests/test_suppression.py` (6 tests, written before any real
  benchmark computation touched corpus data, per SPEC.md's own instruction
  "write the privacy test before the feature"): a 4-distinct-tenant cell
  suppresses; a 5-tenant cell returns a result; 20 observations from one
  tenant never substitute for tenant diversity; metro falls back to national
  when the metro cell alone is too thin; a nationally-thin cell also
  suppresses; percentiles are computed across the full peer set, spot-checked
  with visibly distinct per-tenant prices. All passing.
- `pytest tests/test_negotiation.py` (4 tests): SPEC.md's own worked example
  (a 3% gap on a high-volume SKU outranks a 40% gap on a low-volume one) holds
  under real ranking; SKUs priced at or below peer p25 are excluded (no
  negotiable gap); every line traces to real `invoice_line_item_id`s; the
  sheet caps at 15. All passing.
- **Demoed live** against the seeded corpus: generated a real negotiation
  sheet for "The Copper Skillet" (one of Phase 1's synthetic tenants) — 15
  ranked lines, top one Cilantro (recoverable $255.40/quarter,
  $1,021.60/year, peer benchmark from 12 distinct tenants) — and confirmed
  Cilantro is in fact one of this tenant's injected-creep SKUs (target
  +22.3%), a good end-to-end coherence signal even though the negotiation
  sheet and the creep detector are otherwise independent computations.
- `make validate`'s Phase 0 `make smoke` step is currently blocked by the
  same pre-existing Anthropic account credit issue noted under Phase 2 (the
  real extractor gets a 400 on insufficient credit) — confirmed this is
  unrelated to Phase 4 by running every other gate individually (Phase 1
  `corpus_report`, Phase 2 `test_extract.py`, Phase 3 `matching_report`,
  Phase 4 `creep_report` + both new test files); all green. Full backend
  suite: 74 passed.

## Phase 5 — Dashboard

Status: **done**, gate green, demoed live in the browser.

- Built the three pages SPEC.md §9 lists that didn't already exist (Invoices
  and `/skus` were built during earlier phases; `/skus` isn't one of the four
  but was already there from Phase 3's own demo need):
  - **Review queue** (`frontend/src/app/review/page.tsx`): one item at a
    time, not a bulk table — matches SPEC.md's "the one screen worth making
    genuinely fast." A single autofocused search input drives both actions:
    `Enter` on an empty box confirms the matcher's suggested SKU; typing
    filters canonical SKUs live (reusing Phase 3's `GET /skus?q=` endpoint),
    arrow keys move the selection, `Enter` corrects to the highlighted
    result. Both paths write a `sku_aliases` row (a confirm teaches the
    system too, not just a correction — "the whole point of a human
    verifying a match is that the next identical line resolves for free")
    and, when a price is resolvable, a `price_observations` row — this is
    the real "written after a line item is confirmed" path
    `price_observation.py`'s own docstring describes; Phase 4's
    `seed_corpus_pipeline.py` used `review_status==auto` as a stand-in
    specifically because this endpoint didn't exist yet.
  - **Insights** (`frontend/src/app/insights/page.tsx`): open alerts (refreshed
    on read via `upsert_creep_alerts` — no background job infra in v0), a
    dependency-free inline-SVG sparkline per SKU (`frontend/src/lib/
    sparkline.ts`, unit-tested), and a benchmark position bar (peer
    p25/p50/p75 vs. the tenant's own price).
  - **Negotiation sheet** (`frontend/src/app/negotiation/page.tsx`): the
    ranked table from `build_negotiation_sheet`, a `window.print()` button,
    print-hidden chrome via Tailwind's `print:hidden`.
  - **Invoices detail** (`frontend/src/app/invoices/[id]/page.tsx`): added
    the page image side-by-side with extracted lines that SPEC.md's Invoices
    page calls for and earlier phases hadn't built yet. Required mounting
    `<upload_dir>/renders/` as static files (`app/main.py`) and a new
    `page_image_urls` field on `GET /invoices/{id}` — rendered PNGs existed
    on disk since Phase 0 (`app/ingest/render.py`) but were never served over
    HTTP before this.
- New backend surface: `app/api/{review,insights,negotiation}.py` +
  matching `app/schemas/*.py`, registered in `main.py`. `review.py` reuses
  `app/normalize/matcher.py`'s own `_exact_match_result` to recompute
  normalized price/qty on a correction — not a second reimplementation of
  that pack-size math (the kind of duplication Phase 4's code review flagged
  once already). Also added `GET /review/queue?distributor_id=` as an
  optional filter — genuinely useful for a rep working one distributor at a
  time, and load-bearing for the e2e gate below (see why in that section).
- **Playwright e2e gate** (`frontend/e2e/dashboard.spec.ts`,
  `frontend/playwright.config.ts`): ingest → review → negotiation sheet,
  headless, `npx playwright test` as the only command a human runs (assumes
  `make up` already running, same as every other gate). "Ingest" seeds two
  pending review-queue line items directly via a new
  `backend/scripts/e2e_fixture.py` instead of uploading a PDF through real
  vision extraction — the same "ground truth stands in for extraction"
  reasoning `seed_corpus_pipeline.py` already established for Phase 4, and
  necessary here regardless: the dev environment's Anthropic account still
  has no credit (same blocker noted under Phase 2), so a real-upload e2e test
  would be failing on an unrelated billing issue, not exercising the
  dashboard at all.
  - Test 1 drives both keyboard paths for real: searches and corrects one
    line, confirms another via bare `Enter`, then calls the matcher again
    directly for the corrected line's (distributor, raw_sku) and asserts
    `method == "alias"` — SPEC.md's own definition of "the next matching
    line auto-resolves."
  - Test 2 seeds 20 already-suggested review items and clears all 20 via
    keyboard, timing it against the exit criteria's 120s budget:
    **0.6 seconds** (automated `Enter`-per-item; a human typing would still
    be well inside the budget). Needed `GET /review/queue`'s new
    `distributor_id` filter to get a clean, isolated slice of the queue —
    without it the timing loop raced against whatever else was already
    pending for the dev tenant (real corpus review items, leftover fixtures)
    and undercounted, since pressing `Enter` on an item with no suggested
    match is correctly a no-op.
  - Test 3 checks the negotiation sheet renders a table (or the explicit
    empty state) with a working print button.
  - `backend/scripts/e2e_fixture.py` self-cleans (deletes any prior run's
    `e2e-%`-slugged distributor/invoice/line-items/aliases before creating
    its own) so repeated `npx playwright test` runs — e.g. in CI — don't
    accumulate one throwaway distributor per run forever. Verified by
    running the suite twice and confirming the distributor count stayed flat.
- Added `frontend/vitest.config.ts` (didn't exist before — vitest was
  running with pure defaults) to exclude `e2e/**`: Playwright and Vitest both
  default to scanning `*.spec.ts`, and without the exclusion Vitest tried to
  execute Playwright's `test.describe()` in its own runner and failed.
  `npm run test` had zero unit tests before this phase; added
  `sparkline.test.ts` (4 cases) against a pure function extracted from the
  Insights page's chart component, rather than padding the suite with a
  token test.
- Real bugs found and fixed while wiring this up (not part of any formal
  review pass — just building and immediately checking in the browser):
  - `next build` failed prerendering `/review`: `useSearchParams()` needs a
    Suspense boundary in the App Router or static generation bails with an
    error, not just a warning. Split into an outer `ReviewQueuePage`
    (`<Suspense>`) and inner `ReviewQueueInner` (the actual hook usage).
  - The API server doesn't run with `--reload`, so editing `app/api/*.py`
    mid-session silently kept serving the old code until a manual restart —
    cost real debugging time chasing what looked like a backend filter bug
    when it was a stale process. Worth remembering for any future backend
    edit made while `make up` is already running.
- **Demoed live** in the browser against the seeded Phase 4 corpus data (not
  just Playwright): cleared a real truncated-by-noise line ("2% MILK") via
  keyboard search+correct, confirmed a real embedding-review-band suggestion
  ("AVOCA" → Avocado, 0.91 confidence) via bare `Enter`, watched Insights
  render this tenant's actual injected-creep SKUs (Pasta Penne, Souffle Cup
  2oz, Yeast Active Dry, Bar Mop Towel — matching Phase 1's own injected
  targets almost exactly) with live sparklines and benchmark bars, and
  generated a real negotiation sheet ($976.49 total annualized opportunity
  for one tenant, correctly ranked by dollars not percentage).
- `npm run build`: clean. Full backend suite: 76 passed. `npm run test`
  (vitest): 4 passed. `npx playwright test`: 3 passed, run twice back-to-back
  with no accumulation and no flakiness observed.

### Review pass after Phase 5 (before starting Phase 6)

Ran the code-review skill (high effort) against the Phase 5 diff. 10 findings,
all fixed:

- **Unchecked `Tenant` lookups crashed review/insights actions**: both
  `app/api/review.py` and `app/api/insights.py` called `db.get(Tenant,
  tenant_id)` with no None-check before reading `tenant.metro` — an orphaned
  `tenant_id` (a deleted tenant, or the e2e fixture's own pattern of creating
  line items without a `Tenant` row) raised an unhandled 500 instead of a
  clean 404. Added a shared `_get_tenant_or_404` helper.
- **No pending-status guard let confirm/correct double-write**: neither
  endpoint checked `review_status == pending` before processing, so two
  browser tabs or a retried POST could both succeed against the same line,
  writing duplicate `sku_aliases`/`price_observations` rows. Added
  `_get_pending_line_or_404`, returning 409 on a second attempt.
- **Duplicate open creep alerts under concurrency**: `upsert_creep_alerts`
  was a check-then-act (SELECT existing, then INSERT if none found) with no
  DB constraint backing it. Added Alembic migration 0003 — a partial unique
  index on `price_alerts(tenant_id, canonical_sku_id, alert_type) WHERE
  status='open'` — and rewrote the function as a single atomic `INSERT ...
  ON CONFLICT DO UPDATE` targeting it.
- **`GET /insights` did a full recompute-and-write on every page view**: the
  same `upsert_creep_alerts` call ran unconditionally on every GET, turning
  a nominally safe/cacheable read into a required write, and looped a
  separate price-history query per open alert (an N+1 costing ~60-80 round
  trips for a tenant with 20+ alerts). Moved the trigger to
  `app/api/review.py`'s `_finalize` helper — creep alerts now refresh right
  after a confirm/correct actually lands a new price observation, not on
  every unrelated page load — and batched the price-history query into one
  `WHERE canonical_sku_id IN (...)` covering every open alert.
- **Review-queue fetch race + stale index**: switching the `distributor_id`
  filter mid-session had no guard against an out-of-order response
  overwriting the queue, and `index` wasn't reset on a new fetch — a shorter
  filtered queue could render "Queue is empty" while real items remained at
  a now out-of-range index. Added an `ignore` flag and reset `index` to 0 on
  every new queue fetch.
- **Unclamped `results[selected]` read**: `selected` was kept in sync with
  `results` only by convention (every producer of `results` was expected to
  also reset it); the `Enter` handler read `results[selected]` directly with
  no bounds check. Clamped at the read site instead of trusting every future
  caller to remember the pairing.
- **e2e fixture cleanup was unscoped by tenant**: `_cleanup_prior_fixtures`
  matched distributors by an `"e2e-"` slug prefix alone — two Playwright
  shards running concurrently against different tenants in CI could delete
  each other's in-progress fixture rows. Scoped the query through a join on
  `Invoice.tenant_id` so cleanup only ever touches rows created for the
  tenant currently running.
- **Invoice detail page could crash on a missing field**: `invoice.
  page_image_urls.length` had no guard against `page_image_urls` being
  `undefined` (a new field with zero runtime validation on the fetch).
  Defaulted to `[]` at the read site.
- **`PriceObservation` construction duplicated a third time**: `review.py`'s
  `_write_observation` hand-built the same 8 fields
  `seed_corpus_pipeline.py` already builds inline. Extracted a shared
  `build_price_observation(line, invoice, tenant)` factory into
  `app/models/price_observation.py` (using `TYPE_CHECKING` imports to avoid
  introducing a real cross-model import) and pointed `review.py` at it.
- **Negotiation total computed via JS floats**: the page summed
  Decimal-string `annualized_savings` fields with `Number()` and JS
  floating-point addition — a rounding-drift risk on the one document this
  phase's own docstring says a sales rep will scrutinize line by line. Moved
  the sum server-side (`Decimal` arithmetic, `NegotiationSheetOut.
  total_annualized_savings`) and had the negotiation endpoint's SKU-name
  lookup use one `WHERE id IN (...)` query instead of one `db.get()` per
  line while touching that file anyway.

Verified: full backend suite (76 passed) unchanged; `npm run build` clean;
`npm run test` (4 passed) and `npx playwright test` (3 passed) both green
after restarting the API/frontend dev processes to pick up the changes.

## Full-codebase review pass (after Phase 5, before Phase 6)

Unlike the earlier per-phase diff reviews, this one swept the **current state
of the whole tree** (8 parallel review angles: ingestion/extraction,
normalization, analytics, API/tenant isolation, frontend, synthetic+validation
scripts, cross-phase integration, conventions/cleanup). It was worth doing:
the most serious bug of the whole build only existed *because* several phases
now coexist, so no single phase's own diff review could have seen it. 10
findings, all fixed.

**The big one — the live pipeline never fed its own analytics.**
`app/workers/tasks.py` matched line items and set `review_status`, but never
wrote a `price_observations` row. The only live code that wrote one was
`app/api/review.py`'s confirm/correct — which rejects (409) anything that
isn't `pending`. Since the matcher sets `auto` directly for anything scoring
>= 0.92 (98.8% of lines, per Phase 3's own report), those lines never entered
the review queue and so never produced an observation. Net effect: on real
uploaded invoices, essentially nothing reached price creep / benchmarks /
negotiation sheets — the entire Phase 4 analytics stack had no live data
source, and only the synthetic seed script was feeding it. Both PROGRESS.md's
Phase 5 notes and `review.py`'s own docstring asserted this gap had been
closed; it had only been closed for the minority review-band case.
*Fix:* the worker now writes an observation for every `auto` line (via the
shared `build_price_observation` factory) and refreshes creep alerts once per
invoice. `seed_corpus_pipeline.py` was switched onto the same factory and the
same rule, so the synthetic and live paths can no longer disagree about what
counts as observable. `tests/test_review_api.py` covers this, and the test was
verified to fail without the fix rather than pass vacuously.

**A wrong auto-match was permanent.** `ReviewStatus` has no "disputed" state
and the queue only surfaces `pending`, so any embedding false positive at or
above the auto threshold sat in `price_observations` forever, quietly skewing
every peer benchmark in its cell, with no operator path to fix it. Added
`POST /review/{id}/reopen`, which returns a resolved line to the queue and
deletes the disputed observation so it stops feeding analytics immediately.

**Other fixes:**
- `pack_size.py`'s size groups were `[\d.]+`, which matches nonsense like
  `"4/5.5.5 LB"`; `Decimal()` then raised `decimal.InvalidOperation` — *not* a
  `ValueError`, so it sailed past every `except PackSizeParseError` and failed
  the whole invoice instead of that one line. Tightened to
  `\d+(?:\.\d+)?` (verified: all corpus formats still parse, malformed input
  now raises cleanly) with regression tests.
- `correct_line_item` always marked a line `corrected`, discarding the
  matcher's own `pending` verdict for an unparseable pack size — the line left
  the queue permanently with a null price and no way back. Now respects it.
- An extraction with **zero line items** passed every arithmetic check
  *vacuously* (empty loop, `all([])` is True, `sum([]) == 0` reconciles against
  zeroed totals) and shipped as `extracted`. Now explicitly routed to
  `needs_review`.
- `matcher.py` still hardcoded its confidence thresholds behind a "mirrors
  thresholds.yaml" comment — the exact anti-pattern `price_creep.py`'s
  docstring calls out **by name** as already-fixed, never backported to the
  file it originated in. Three of the eight review angles independently
  flagged it. Now loaded from the YAML like price_creep.py's.
- Review queue: if the distributor filter changed while sitting at index 0,
  `setIndex(0)` was a no-op, the `[index]` reset effect never re-fired, and a
  stale search dropdown from the *previous* item stayed on screen — one Enter
  away from applying a correction to a different line item. Form state is now
  cleared when the queue itself changes.
- Tenant validation was inconsistent across the five routers (some 404'd, some
  returned a degenerate 200, and `upload_invoice` would have raised an
  unhandled `IntegrityError` *after* writing the uploaded file to disk).
  Consolidated onto one `app/api/deps.py::get_tenant_or_404` used everywhere.

Verified: 83 backend tests pass (up from 76). Phase 3 `matching_report`
(98.8% auto-match, 0% false match) and Phase 4 `creep_report` (93.9% recall,
0 false positives) both still green after reseeding. `npm run build` clean,
vitest 4 passed, Playwright 3 passed. The corpus reseed produced slightly
*more* observations than before (38,709 → 38,748) — not a regression: the
`sku_aliases` rows written during earlier manual review testing now
auto-resolve lines that used to fall to the review queue, which is exactly
the compounding effect the alias table exists for.

## Phase 6 — Email intake

Status: **done**, gate green, demoed live in the browser.

- `app/ingest/email_stub.py` (the filename SPEC.md §3's repo layout reserved):
  parses `.eml` files from a watch directory, routes each to a tenant by the
  address it was sent to, and turns every PDF attachment into an invoice on
  the same queue the HTTP upload endpoint uses. Nothing talks to a mail
  provider — swapping in a real inbound webhook later means replacing
  `scan_inbox`, not the routing/attachment logic under it.
- **Per-tenant address routing** needed somewhere to route *to*, so
  `tenants.inbox_address` is new (migration 0004): nullable, because a tenant
  can exist before an address is issued, and unique, because it IS the
  routing key. The unique constraint immediately earned itself — backfilling
  addresses across existing tenants failed on the duplicate-named leftover
  test fixtures (`Test Tenant` x N) rather than silently making inbound mail
  for them ambiguous.
- Routing reads `Delivered-To`/`X-Original-To` as well as `To`/`Cc`, because
  the realistic flow is a restaurant *forwarding* a distributor's invoice —
  which leaves their own address in `Delivered-To` while `To` becomes
  whoever they forwarded it to. Matching is case-insensitive.
- **Nothing is silently dropped** (the spec's actual exit criterion). An
  email that can't be routed, carries no PDF, or fails to parse is moved to
  `inbox/quarantine/` with a `.reason.txt` beside it. Successful ones move
  to `inbox/processed/`. Both use a collision-safe destination name, so the
  same invoice forwarded twice doesn't overwrite the first copy — and
  because the inbox is left empty either way, a rerun can't double-ingest.
- Multi-attachment: one invoice per PDF (a distributor mailing a week's
  invoices as several attachments is one email but several invoices);
  non-PDF attachments like signature logos are ignored, but an email with
  *only* non-PDFs quarantines rather than vanishing.
- `make watch-inbox` polls the directory (`ONCE=1` scans and exits). Polling
  rather than inotify/FSEvents on purpose: a few lines, identical on every
  platform, and the interval stops mattering the moment a real webhook
  replaces it.
- Small refactors this phase, both to avoid a second copy of something:
  `app/queue.py` now owns the one Redis/RQ queue (the upload endpoint was
  constructing its own, and email intake would have been a second), and
  `upload.py` grew `save_invoice_bytes` that `save_uploaded_file` delegates
  to, so email attachments and HTTP uploads name and place files identically.
- `pytest tests/test_email_intake.py`: 11 passed — routing by address,
  case-insensitivity, `Delivered-To` forwarding, unroutable → quarantine
  (asserting the file and its reason survive, not just that no invoice was
  created), multi-PDF → multiple invoices, mixed attachments, no-PDF →
  quarantine, malformed email → quarantine rather than crash, scan-level
  failure isolation, and the same-filename collision case.
- **Demoed live**: dropped two `.eml` files in `inbox/` (one addressed to
  Blue Oak Kitchen with two invoice PDFs plus a logo.png, one addressed to
  nobody), ran `make watch-inbox ONCE=1`, and watched it report
  `2 invoice(s) for tenant …` and `QUARANTINED — no tenant for recipient
  address(es): wrong-address@…`. Both invoices then appeared at the top of
  the dashboard with `source: email`, and the detail page showed the actual
  emailed PDF rendered side-by-side with its extracted line items.
- Note on that demo: the running RQ worker first marked both `failed` on the
  same Anthropic account-credit blocker carried since Phase 2 — unrelated to
  email intake, which had already done its job. Re-running the two
  email-created invoices through the `FakeExtractorClient` (the documented
  dev path when no API key is configured) took them to `extracted` and
  produced the screenshots above.
- Full backend suite: 94 passed. Phase 3 `matching_report` and Phase 4
  `creep_report` still green; Playwright 3 passed.

## Post-Phase-6 — History-basis negotiation sheet, and the multi-unit benchmark bug

Two things, driven by the same question: what is this product worth to a
customer on day one, before any peer density exists?

### The bug: suppression counted locations, not businesses

SPEC.md §7 words the privacy rule as "fewer than 5 distinct tenants." A
tenant is a *location*. A five-location restaurant group onboarded as five
tenants therefore cleared its own suppression threshold using nothing but its
own locations, and the "peer benchmark" it got back was the group compared
against itself — a wrong number and a silent defeat of the rule that produced
it. `exclude_tenant_id` had the same hole one level down: excluding only the
asking location still left its siblings' prices in the cell.

- New `accounts` table and a nullable `tenants.account_id` (migration `0005`).
  A tenant with no account is its own account, so this is a no-op for every
  single-location customer and for every row that predates it — no backfill.
- `app/analytics/benchmark.py` counts `COALESCE(tenants.account_id,
  tenants.id)` and `exclude_tenant_id` became `exclude_account_key` (resolved
  once per request via `account_key_for`, not once per SKU).
- Joined to `tenants` rather than denormalizing `account_id` onto
  `price_observations` beside `metro`/`volume_tier`. Those two are deliberate
  snapshots of what was true when the line was billed; account membership is a
  privacy fact that has to be *current*, or a group acquiring a restaurant
  today would keep counting as that restaurant's peer for last month's prices.
- Verified against the live corpus in a rolled-back transaction: a Columbus
  cell with exactly 5 distinct tenants read p25 $17.8530 (metro). Making those
  same 5 tenants one account suppressed the metro cell and fell back to
  national — 11 real accounts, p25 $19.1386. The $1.29 difference is how much
  of that "benchmark" was the group's own prices.
- The UI now says "businesses" rather than "tenants" everywhere the count
  surfaces, because that is now what it counts.

### The feature: a negotiation sheet that needs no peers

The peer basis needs 5 independent businesses buying the same SKU in the same
metro. A customer in a thin metro may wait months for that, or never get it —
which is exactly the customer with nothing else to show them. So the sheet
grew a second basis, and `basis=auto` (the default) picks per SKU:

- `peer` — target is peer p25, as before.
- `history` — target is the **median of the tenant's own prior prices** for
  that SKU. Live from the fourth delivery of an item, with zero peers.
- Each line carries the basis it used, and the page labels it per row ("peer
  p25 · 12 businesses" vs "your median · 7 priors"). A sheet that mixed the
  two silently would be quoting two different claims under one header, and the
  rep across the table finds that seam.

Median, not p25, for the history target — and this was worth getting wrong
once to find. A quartile over a handful of one tenant's own purchases is an
interpolated number no invoice ever showed: priors of $2.00/$10.00/$10.00 (one
spot buy, SPEC.md §4's `off_contract`) interpolate to a $6.00 p25 that a rep
kills with "when did you ever pay that?" The median answers $10.00, which is
both true and the number worth arguing from. It is also the same statistic
`price_creep.py` uses for its baseline, so a SKU's Insights alert and its
negotiation line can no longer quote two different "before" prices.

The current price is excluded from the history target it is measured against,
for the same reason the peer basis excludes the asker's own account: otherwise
today's overpayment quietly raises the bar it is being judged by.

### The annualization was wrong in the safe direction, which is still wrong

`ANNUALIZATION_FACTOR = 4` ("a 90-day window is ~1 quarter") is only true once
a customer has 90 days of invoices. Before that it took a partial window's
quantity and multiplied it by 4 — a tenant 30 days in had a month of purchases
projected as a third of their real annual exposure. Understating is the safe
direction for a claim a rep will attack, but it landed hardest on new
customers, who are precisely the ones with no peer benchmark either.

Now projected from the span actually present, with the denominator floored at
`min_annualization_days: 28` so three deliveries aren't extrapolated 100x.
`window_days` and `annualization_factor` are returned on the sheet and printed
under it — every dollar above is a projection from that much history, and the
person on the other side of the table is entitled to know how much.

Measured on the corpus (Riverside Trattoria, as_of 2026-08-24): 85 days of
history, peer basis $76,213/yr, history basis $21,286/yr from the same
invoices with no peers at all. Rewound to `as_of=2026-03-30` — a customer 29
days in — the history sheet still returns 15 lines and $6,931/yr, at a 12.59x
factor. The old flat 4x would have called that $2,203.

### Gates
- Backend: 112 passed (was 94). 18 new tests, covering the group-suppression
  cases (one group of five is suppressed; a group counts once; excluding the
  asker excludes its siblings), the history basis, and the annualization
  floor. The suppression tests genuinely fail against the old tenant-counting
  code — that is the point of them.
- `validation.creep_report`: recall 93.9%, 0 false positives. PASS.
- Playwright: 4 passed (one new — the history basis renders with no peers).
- Frontend `tsc --noEmit` clean, vitest 4 passed.
- Verified in the browser on both bases plus the Insights page.

### Lessons
- Two stale dev servers bit again: the API had to be restarted to pick up the
  new `basis` param (same no-`--reload` lesson as Phase 5), and a leftover
  Next.js on :3001 made Playwright fail with `EADDRINUSE` before a single test
  ran.
- The live page caught something the tests did not: an even-length median
  averages two prices and lands on a 5th decimal, so the sheet rendered
  "$6.03395" in a column of 4-decimal money. Quantized like everything else.

### Code review of Phase 6 + the negotiation work (7 findings, all fixed)

First review to cover Phase 6, which had shipped without one.

1. **`auto` never fell back to history once a peer cell existed.** The basis
   was decided on "a peer cell exists," not "a peer cell has something to
   argue," so a SKU priced *under* peer p25 was dropped outright and its own
   creep never looked at. Now both candidates are evaluated and peer wins only
   when it actually shows an overpay. Measured across the corpus: **293 of
   2,626 SKU-tenant pairs (11%)** sit under peer p25 while still above their
   own median, and **5 of 20 tenants' visible top-15 sheets** gained history
   lines — Cedar Table gained 12 of 15, including a $4,580/yr line that was
   invisible before.
2. **Email intake committed invoices before moving the file and enqueuing.**
   A Redis outage threw into `scan_inbox`'s handler, whose `db.rollback()` was
   a no-op against the already-landed commit, so the email was filed as
   "ingest failed" with its invoices sitting in the database — and re-dropping
   it, the documented recovery, made a second set. The email is now *claimed*
   (moved to `processed/`) before any write, a failed write quarantines from
   there, and enqueue is last and non-fatal: the invoices are durable by then,
   so a queue outage is reported as a warning on an ingested result rather
   than re-opening the question of whether the email was handled.
3. **The failure-isolation handler could itself throw.** If `_quarantine`
   failed *after* its rename (disk full writing the `.reason.txt`), the outer
   handler's second `path.rename` raised `FileNotFoundError` and took down the
   whole scan — stopping every email behind it, the exact outcome that handler
   exists to prevent. Added `_safe_quarantine`, which reports instead.
4. **The scan raced files still being written.** A half-copied `.eml` parses
   without raising (MIME parsing is tolerant), yields no attachments, and was
   moved to quarantine as "no PDF attachment" while the writer was still
   appending. `scan_inbox` now ignores files touched within
   `INBOX_SETTLE_SECONDS` (2s); skipping drops nothing, the file stays for the
   next pass.
5. **`message_id` was parsed and thrown away, so redelivery duplicated
   invoices** — and duplicate invoices double-count into the benchmark cells
   and creep windows the analytics depend on. Added `invoices.source_message_id`
   (migration `0006`, indexed not unique — one email can legitimately carry
   several invoice PDFs) and a per-(tenant, message-id) check. A redelivered
   email is filed as `processed` with status `duplicate`, deliberately *not*
   quarantined: quarantining invites the operator to re-drop it and try the
   duplicate again. `subject` was the other unused field; it now rides along in
   every quarantine reason so a human can recognise the message without
   opening the `.eml`.
6. **N+1 benchmark queries on the default page.** `compute_benchmark` ran
   once per SKU inside the sheet loop. Added `compute_benchmarks` (two queries
   for any number of SKUs, metro then national fallback over what's still
   unresolved), with the singular form kept as a thin wrapper for the insights
   page, which genuinely has one SKU per alert window. **5 SQL statements per
   sheet, down from up to 239**; 319ms → ~60ms on the current corpus.
7. **A failed fetch wedged the negotiation page on "Loading..." forever**,
   reachable by clicking a basis button while the API restarts. Added the
   missing `.catch`, and while verifying the fix in the browser the fallback
   turned out to claim "No overpriced SKUs" when the truth was "couldn't
   reach the server" — a bad thing to tell someone walking into a pricing
   conversation, so failure is now its own state with its own message.

**A correction worth recording.** The evidence I first attached to finding 1
was wrong. I reported "6 of 15 history lines missing from the auto sheet,
including Vegetable Oil with a live 16.7% creep alert" — but those 6 were
missing because of the `TOP_N = 15` cap, not the basis bug, and Vegetable Oil
had a peer line all along. The defect was real (the synthetic test fails
against the old code, and 293 real pairs hit it), but I'd have shipped a
confident, checkable claim that didn't hold. The habit that caught it was
re-running the *same* measurement after the fix and not accepting "still 6"
as noise.

### Gates after the fixes
- Backend: **121 passed** (was 112). New tests verified to fail against the
  pre-fix code by reverting each fix in turn: 4 email-intake tests and 2
  negotiation tests went red, then green again on restore.
- `creep_report` 93.9% recall / 0 FP, `matching_report` 0 unparseable / 0%
  false match — both still PASS.
- Playwright 4 passed, vitest 4 passed, `tsc --noEmit` clean.
- Browser: default sheet now renders a genuine peer/history mix (10 history
  lines interleaved by dollar value), and the API-down path was exercised
  live by killing uvicorn mid-session.

## Peer-price spectrum, and the weighting bug it exposed

### The visual

The Insights benchmark readout was a 2px-tick bar plus a line of four dollar
figures, which made the reader do the comparison themselves. Replaced with a
spectrum: a plain-language verdict ("More expensive than 91% of 11 comparable
businesses · 91st percentile"), a green→red track with the quartile ticks, and
a pin for the reader's own price.

- **The track is a percentile axis, not a dollar axis.** A dollar axis has to
  be rescaled per SKU, so every card's quartile ticks land somewhere different
  and you can't scan a page of alerts. On a percentile axis the ticks are in
  the same place on every card and the pin's position means the same thing
  every time. Magnitude hasn't gone anywhere — the card still states the price,
  the move, and the quartile dollars under their own ticks. (Built the dollar
  version first; switching only became obviously right once four real cards
  were on screen next to each other.)
- **New API field: `BenchmarkPosition.percentile`**, from
  `BenchmarkResult.subject_percentile`. Deliberately a *rank*, not another
  published quantile: with as few as 5 businesses in a cell, a p10 or a min
  would effectively be one identifiable competitor's price, which SPEC.md §1
  forbids. A rank is a property of the asker's own price and reveals nothing
  p25/p50/p75 don't. Ties count as half, so matching every peer reads as the
  50th percentile rather than as 0 or 100 depending on comparison direction.
- Geometry and wording live in `src/lib/spectrum.ts` with 11 unit tests, the
  same split `sparkline.ts` already uses — a marker in the wrong place is a
  wrong answer that looks authoritative.

### The bug it exposed: percentiles weighted by delivery frequency

Building the percentile rank meant asking what the denominator actually was,
and it was wrong. Percentiles were taken over **raw observations**, so a
business that takes weekly deliveries got 13 times the votes of one that
orders monthly. That was always a bit off; the account change made it a real
defect, because suppression now counts a multi-unit group **once** while the
statistic still took **every location's** prices.

Simulated, then fixed: a cell of one five-location group at $20.00 plus five
independents at $10-$14 returned a **p25 of $20.00** — a "target price" that
argues the customer should pay *more* than every independent in the market.

`_cells` now collapses each account to one representative price (its median,
matching the creep baseline and the history target) before computing
percentiles. One business, one vote — which is also what makes the new
percentile claim ("more than 78% of comparable businesses") literally true.
Corpus effect: the negotiation sheet total moved $76,213 → $74,092 (-2.8%),
i.e. slightly more conservative, which is the right direction.

### Other issues found and fixed in the same pass
- `insights.py` did `db.get(CanonicalSku, ...)` per alert — the N+1 that
  `negotiation.py` already had a comment explaining it avoided. Now one query.
- `BenchmarkPosition.percentile` is required, so a None would have 500'd the
  whole page. Can't happen today (alerts carry a non-null `current_price`),
  but the card now fails closed: no percentile, no spectrum.
- The "you $X" label overflowed the card at the 100th percentile — exactly the
  case a reader most needs to read. Label and pin are now positioned
  separately so the label can hug the edge without moving the pin off its
  percentile (`labelAnchor`, tested).
- "Higher than 100% of 12 comparable businesses" is not a sentence anyone
  says; the extremes are now named ("More expensive than all 12...").
- Pre-existing mobile layout: the fixed 220px sparkline sat beside the text and
  crushed that column to ~140px at 375px wide, wrapping the SKU name and price
  line onto six lines each. Now stacked below `sm:`.
- Two redundant `sorted()` calls passed into `statistics.median`, which sorts
  internally.

### Gates
- Backend 125 passed (was 121): 4 new — frequent-buyer weighting, group
  weighting, percentile rank including the tie case, and rank absent when no
  subject price was given.
- Frontend 15 unit tests (was 4), `tsc` clean, Playwright 4 passed.
- `creep_report` 93.9% / 0 FP and `matching_report` 0/0 both still PASS.
- Verified in the browser at desktop and 375px, both bases of the negotiation
  sheet, and with the API stopped.

### Lesson
Next's dev server served a 404 for `/insights` after a hot-reload runtime
error mid-edit, while the same source built and passed 4/4 in Playwright on
its own server. `rm -rf .next` and a restart fixed it. Worth knowing before
debugging a routing problem that isn't one.

## SPEC.md §12 Q1 — how far does one correction travel?

The spec asked: "Should a corrected alias apply across all tenants
immediately, or only after N confirmations? (Leaning: cross-tenant after 2
independent confirmations.)" It had never been answered. `sku_aliases` had no
tenant at all, so the answer in force was **immediately, off one person's
click** — a single mistaken correction silently rewrote matching for every
other customer, in the table this repo's own model docstring calls the moat.

Resolved as the spec's leaning said. `sku_aliases.tenant_id` (migration
`0007`) records who corrected what, and `match_by_alias` trusts an alias for a
given asker when, in order:

1. **Their own business made it** — your correction applies to you
   immediately. Keyed on *account*, so a group's other locations count as the
   same business, and a sibling location inherits it at once.
2. **It is system-curated** (`tenant_id` NULL — a catalog import, not one
   person's judgement). This is also what every pre-existing row means, so the
   migration changes no existing behaviour.
3. **At least `min_independent_alias_confirmations` (2) separate businesses**
   made the same mapping.

Counting businesses rather than tenants for the same reason benchmarking does:
five locations of one chain agreeing is one opinion. `account_key_column` moved
from `benchmark.py` to `app/models/tenant.py`, since two unrelated layers now
need the same notion of "independent business" and `normalize` importing from
`analytics` would have been backwards.

**Contradictory mappings resolve to None, not to the more popular one.** Two
camps of businesses disagreeing about what a distributor's code means is
exactly where guessing produces a confident false match; falling through costs
one embedding search and protects SPEC.md §6's false-match budget. A clear
majority (3 vs 2) does win — it's only a tie that abstains.

Within each tier the **newest** correction wins, which is a partial answer to
the spec's third open question (a distributor reassigning an item code
mid-year): a tenant who re-corrects the same code means the later answer.

### Data hygiene this surfaced
- `test_alias.py` defined its own committing `db_session` that shadowed
  conftest's rollback fixture — the source of **103 stray `sku_aliases` rows**
  in the dev database. Removed; the module now uses conftest's.
- Those 103 were deleted, and the **16 real corrections from the Phase 5 demo**
  were attributed to the tenant whose line items produced them, so they stop
  being grandfathered as globally-trusted curated aliases. One-off cleanup, not
  a migration: a fresh deployment has none of these rows.
- The new FK exposed a hole in `test_review_api.py`'s teardown: it deleted
  tenants before the aliases the **confirm/correct endpoints** had written on
  their behalf. Those were never in `created["aliases"]` (the API made them,
  not the test), so before the FK they leaked silently and after it they broke
  the teardown outright. Two had already leaked — and being the same mapping
  owned by two different tenants, they counted as an independent confirmation
  and promoted the alias globally. Test debris acting as crowd evidence.

### The stale-server lesson, finally fixed at the root
A backend edit looked like it hadn't worked (new aliases still had no tenant)
because the running uvicorn predated the edit — the third time this session,
already recorded twice. `make up` now starts uvicorn with `--reload`. It is
the dev harness; a file watcher is cheaper than debugging the same illusion a
fourth time.

### Gates
- Backend **134 passed** (was 125). 9 new alias tests; 4 of them verified to
  fail against the old first-row-wins behaviour.
- `matching_report` 0 unparseable / **0.0000% false match (0/38709)** — the
  gate that would catch a promotion rule that trusts too much.
- `creep_report` 93.9% / 0 FP, Playwright 4 passed, frontend 15 unit tests.
- The e2e "a correction auto-resolves the next matching line" now exercises
  rule 1 specifically: `verify-alias` takes a tenant, because "who's asking"
  is the whole question now.

### Still open
- SPEC.md §12 Q3 proper (a distributor reassigning an item code to a genuinely
  different product) — only the same-tenant re-correction case is handled.
- Phase 2's extraction gate has still never run against the real API.
- Accounts remain schema-only: no way to create one or assign tenants.

## Accounts made usable, and SPEC.md §12 Q3's dangerous half

### Q3: a distributor reusing a retired item code

The spec asks "How do we handle a distributor changing an item code for the
same product mid-year?" The same-product direction was already covered (a
tenant's newer correction wins). The direction that actually loses money is
the reverse: a distributor **reuses** a retired code for a *different*
product. The alias path short-circuits before any embedding call, so every
line for that code kept matching the old product at confidence 1.0 — no
similarity check to notice, no human ever seeing it, and cheddar's prices
recorded as mozzarella's in every benchmark cell and creep window downstream.
A silent, confident false match, which SPEC.md §1 names as the worst failure.

`match_by_alias` now checks, per row and before any counting, that the
incoming description still describes the same item
(`min_alias_description_similarity`). A reassigned code simply stops
qualifying and resolves on its own merits. Being too strict costs one
embedding search, never a wrong answer — which is why the threshold sits
where legitimate variation never reaches it.

**Getting the similarity measure right took two attempts, and the corpus
caught the first one.** Exact-token Jaccard looked obviously fine and was
badly wrong: distributors truncate descriptions to a column width, so
"CUCUMBER" prints as "CUCU" and "BACON SLICED" as "BACON S". Measured across
810 item codes, a code's *own* descriptions scored a median of 0.333 and the
CUCU pair scored 0.000 — a threshold anywhere useful would have disabled the
alias path wholesale rather than catching reassignments.

Replaced with prefix-tolerant symmetric coverage, which reads truncation as
the same word. Then the first unit test failed and caught a second problem:
many distributors print the pack into the description, and the shared "4 5 LB"
tokens scored a mozzarella against a cheddar at 0.615 — enough to hide a
reassignment. Numeric tokens are now dropped (pack size has its own parser and
its own place in the pipeline; it is not product identity).

Final calibration, 810 codes and 16,000 cross-code pairs:

| | same item code | different item codes |
|---|---|---|
| min / median | 0.500 / 1.000 | — / 0.000 |
| p99 | — | 0.500 |

At `0.50`: **0.00% of legitimate alias hits skipped, 98.6% of reassignments
caught.** `matching_report` still reports 0.0000% false match (0/38709).

### Accounts, reachable at last

Benchmark suppression and alias promotion both hinge on accounts, and both
shipped before there was any way to create one — the privacy fix was
unreachable by an actual operator.

- `POST /accounts`, `GET /accounts`, `GET /accounts/{id}`,
  `POST /accounts/{id}/locations`, `DELETE /accounts/{id}/locations/{tenant}`,
  plus `GET /tenants?unassigned=true` for the picker.
- **Re-parenting is a 409, not a silent move.** Shifting a location between
  businesses changes which cells it can corroborate and whose corrections it
  inherits; detaching first makes that deliberate.
- A `/accounts` page, deliberately set apart in the nav: it is the one
  operator-facing screen, working across tenants rather than inside the one
  the dashboard points at. It leads with *why* grouping matters rather than
  presenting itself as bookkeeping.
- **No auth on any of it**, same as the rest of v0 (SPEC.md §1). Noted in the
  module docstring because it matters more here: detaching raises the
  distinct-business count and can un-suppress a cell that was suppressed a
  moment earlier. First thing to gate when there is anything to gate against.

The last test is the one worth having: five locations clear a five-business
threshold, then the API groups them, and the same cell suppresses.

### Dev-database hygiene
46 of 67 tenants were debris from test runs that predated conftest's rollback
fixture, which made the location picker unusable and is the same root cause as
the 103 stray aliases cleaned up earlier. Removed, with their invoices and
line items. The demo account created while verifying the UI was removed too —
a stray grouping would sit in every future benchmark computation.

### Gates
- Backend **144 passed** (was 134): 7 accounts-API tests, 3 reassignment tests.
- `matching_report` 0.0000% false match (0/38709), `creep_report` 93.9% / 0 FP.
- Playwright 4, frontend 15 unit tests, `tsc` clean.
- Verified in the browser: created a business, attached a location through the
  picker, confirmed the API recorded it.

### Still open
- Phase 2's extraction gate has never run against the real API. Unchanged, and
  now the only substantial item left.
- `min_alias_description_similarity` is calibrated on synthetic descriptions.
  Real distributor variation is probably wider; the failure mode of being too
  strict is benign (one embedding call), so erring strict was the right call,
  but this is worth re-measuring on real invoices.

## Full-codebase review, all phases (8 findings, all fixed)

The three most serious put wrong numbers into analytics, and all three were
invisible to every gate: the corpus substitutes ground truth for extraction and
bills every line by the case, so none of these paths ever ran on bad input.

1. **Unverified prices fed analytics.** The worker wrote observations for
   auto-matched lines *before* the arithmetic check, so an OCR-misread price on
   an invoice headed for `needs_review` still reached benchmarks and creep
   alerts. Confirming a line from such an invoice in the review queue did the
   same. Fixed in `build_price_observation`, the one factory every writer uses:
   it now refuses anything whose invoice isn't `extracted`, and the worker
   assesses before it persists. Identity can be certain while the price isn't.
2. **Creep alerts never closed.** `upsert_creep_alerts` only added or updated,
   so an alert outlived its cause forever — including the one `reopen` was
   meant to clear by recomputing. Open creep alerts whose SKU no longer shows
   creep are now `resolved`; acknowledged or dismissed ones are left alone.
3. **Broken-case billing recorded at the wrong unit.** Any non-case line was
   assumed to be priced per base unit, so one 5 lb bag billed "EA" at $12.50
   became $12.50/lb (5x), and "LB" against a can pack 16x. Pass-through now
   requires the billed unit to *be* the pack's unit; otherwise the line keeps
   its SKU suggestion but gets no price, can't auto-resolve, and can't write an
   observation (`BilledUnitMismatchError`, a `PackSizeParseError` so every
   caller already routes it to review).
4. **A post-commit failure marked a live invoice `failed`,** and retries
   duplicated line items. The alert refresh now runs outside the failure
   handler and is non-fatal; completed invoices are skipped and a retry clears
   what an interrupted attempt left.
5. **Reopen kept the disputed alias,** which re-applied the wrong mapping at
   confidence 1.0 on the next invoice. Now removed with the observation.
6. **Review search race:** a late response for an earlier keystroke could
   replace the results, one Enter from a wrong correction. Only the newest
   request may set results now.
7. **Spend on failed extractions was never recorded** — two billed calls on
   exactly the hard invoices the cost gate most needs to see.
   `ExtractionFailedError` carries the cost and the worker persists it.
8. **Uploads had no size or type check.** Now refused before touching disk
   (413 over `max_upload_bytes`, 415 if the bytes aren't a PDF). Email intake
   decides PDF-ness from the bytes too, not the sender's label.

### Gates
- Backend **160 passed** (was 144). 16 new tests; with the source fixes stashed
  and the tests kept, all 10 behavioural ones fail, and the pack-size file
  can't import. The remaining new tests are guards that pass either way.
- Seeded observations unchanged at 38,713 — the new gate doesn't touch
  validated invoices. `creep_report` 93.9% / 0 FP, `matching_report` 0/38709.
- Playwright 4 (exercises the changed search path), frontend 15 unit, `tsc` clean.

## Invoice review screen — the way back out of `needs_review`

The previous fix (unverified prices no longer feed analytics) left a dead end
behind it: an invoice whose numbers failed the arithmetic check could never
feed analytics again. The line-level review queue settles *which SKU* a line
is, not whether its numbers were read correctly, and `InvoiceStatus.confirmed`
existed in the schema with nothing ever setting it. Invisible in dev (527
`extracted`, 0 `needs_review`, because synthetic data never fails arithmetic),
but every real invoice with one OCR error would have silently dropped out of
every benchmark, alert and negotiation sheet.

- **Same rules, one implementation.** The arithmetic core moved out of
  `assess_extraction` into `check_arithmetic`, which the worker and the review
  screen both call. Two versions of "does this add up" would eventually
  disagree, and then an invoice could be confirmed that the worker rejected.
  The stored check deliberately drops the extractor's self-reported per-line
  confidence: a person reading the page is what replaces that.
- **`PATCH /invoices/{id}`** corrects header and line numbers, date and
  distributor. Re-pricing a line re-derives its price per base unit from the
  corrected number (`normalize_price`); the match stands, since identity
  doesn't depend on price. Changing the distributor re-matches every line —
  item codes only mean something within one catalog, so even a line someone
  already confirmed was confirmed against the wrong one. All validation runs
  before any mutation; values are bounded to the columns' `Numeric(12,4)`.
- **`POST /invoices/{id}/confirm`** requires the check to pass, sets
  `confirmed` (which `build_price_observation` now accepts alongside
  `extracted`), and writes the observations its settled lines would have
  written. Pending lines get theirs when the line queue resolves them. Row
  lock against double-confirm; the alert refresh is non-fatal.
- **Only `needs_review` invoices are editable.** An `extracted` invoice's
  numbers already feed analytics; rewriting them in place would silently move
  benchmarks other tenants read.
- **The screen:** the detail page shows why the invoice was held, highlights
  the failing lines, and makes the numbers editable. Confirm stays disabled
  while there are unsaved edits, because the check on screen is of the saved
  numbers. The invoice list gets status badges and a banner naming the
  invoices being held out of analytics. Money stays a string end to end; the
  browser never does arithmetic on it.

**A bug I caught in my own first version:** the worker stores extraction's
"other" as a real distributor row, not NULL, so checking `distributor_id is
None` would have let an invoice nobody could attribute be confirmed. "other"
now counts as unrecognized, isn't offered in the picker, and is refused as a
correction.

### Gates
- Backend **172 passed** (was 160; 12 new review tests). Playwright **5**
  (new: correct a misread price in the UI, save, confirm).
- Verified by hand in the browser: a misread $74.50 corrected to $47.50 wrote
  its observation at $2.3750/lb, not the $3.7250 the misread would have given.
- `creep_report` 93.9% / 0 FP, `matching_report` 0/38709, 15 frontend unit.
- 108 more debris distributors from pre-conftest test runs removed from the
  dev DB; they were cluttering the new distributor picker.

### Still open
- `failed` invoices (no line items at all) still have no recovery path beyond
  re-uploading.
- No audit trail of who corrected which number — worth adding with auth.

## Entering line items by hand

The invoice review screen could only edit lines that already existed, which
left two dead ends: an invoice where extraction found no line items (held for
review as "no line items", with no way to add the first one) and a `failed`
invoice (not editable at all). Both are the "nothing was detected" case.

- **"+ Add line item" opens a modal** on any invoice under review, and now on
  `failed` ones. A type-ahead searches this tenant's own past purchases from
  the same distributor (all of the tenant's distributors if it's
  unrecognized), one entry per item at its most recent purchase. Picking one
  fills description, item code, pack size and unit.
- **Price is a hint, never prefilled** (the user's call, on my
  recommendation): "Last paid $587.71 per CS on 2026-08-17. Type the price
  this invoice shows, even if it's the same." A prefilled price left untouched
  would record "no increase" and hide exactly the creep the product exists to
  catch. The hint comes only from invoices whose numbers passed the check, so
  it can't be a misread price. The extended price isn't computed either:
  every printed number is typed, so the arithmetic check can catch a typo
  rather than agree with a value derived from it.
- **Hand-entered lines go through the ordinary matcher**, so a line picked
  from history resolves the way that purchase did (the tenant's own alias, or
  the same embedding match). No client-supplied "copy this match" id to trust.
  Lines can also be removed, for a mistyped entry or one extraction invented.
- **Editing a `failed` invoice moves it to `needs_review`.** Not cosmetic: the
  worker retries `failed` invoices and clears leftover lines before
  re-extracting, so a re-enqueued job would have deleted lines someone just
  typed in. `needs_review` is one the worker leaves alone (tested).
- With no lines yet, the check reports only "no line items" instead of also
  "lines don't add up to the subtotal", and the guidance says to add lines.

**Fixture hygiene this surfaced:** the e2e fixtures confirm invoices on real
catalog SKUs in the tenant's real metro, and were only cleaned up at the
*start* of the next run, so the last run's prices sat in real benchmark cells
and creep windows in between. A new `cleanup` command runs in the suite's
`afterAll`; zero E2E invoices remain after a run.

### Gates
- Backend **181 passed** (was 172; 9 new). Playwright **6** (new: open the
  modal, pick a past purchase, assert the price field is empty, enter the
  line and totals, confirm). Frontend 15 unit, `tsc` clean.
- `creep_report` 93.9% / 0 FP, `matching_report` 0/38709.
- Driven by hand in the browser against real purchase history.

## Second full review (8 findings, all fixed)

1. **The PDF check rejected valid PDFs.** It required `%PDF-` at byte 0, but
   readers accept it anywhere in the first 1024 bytes, and real files arrive
   with a BOM or a stray newline in front. Uploads got 415 and emails were
   quarantined as "no PDF attachment". Now searches the first 1024 bytes.
2. **"other" was matched and aliased as if it were a catalog.** The worker
   stores extraction's "other" as a real row, then matched lines against it,
   and line-queue corrections wrote aliases under it, pooling item codes from
   every distributor that ever reached "other". Both paths now skip it, as the
   review screen already did.
3. **Re-attributing an invoice left corrections under the wrong distributor.**
   Fixed at the root rather than by guessing: aliases now record the line they
   were written from (`sku_aliases.source_invoice_line_item_id`, migration
   `0008`, ON DELETE SET NULL). Changing an invoice's distributor withdraws
   exactly the aliases its own lines produced, and leaves the same tenant's
   identical correction from a genuine invoice alone — deleting by
   (distributor, item code) would have taken both.
4. **A retry already in flight could wipe hand-entered lines.** Moving a
   failed invoice to `needs_review` on edit only stopped retries that started
   after the edit. The worker now takes the invoice row `FOR UPDATE`, the same
   lock the review endpoints take, so the status check and the clear are one
   step. Tested with two real sessions: the worker blocks on the reviewer's
   lock, then sees `needs_review` and leaves the lines alone.
5. **Email skipped the upload size limit.** Both entry points now go through
   `validate_invoice_bytes`; an oversized attachment quarantines the email
   with a reason naming the file.
6. **The upload form said only "Upload failed."**, discarding the API's
   reason (not a PDF, too large). It shows the reason now.
7. **The review screen hid which cell failed validation.** A shared, tested
   `formatApiError` (used by the upload form, review screen and modal) names
   the field, maps `line_items[i]` back to the line number on screen, and
   rewords "valid decimal" as "enter a plain number, like 1234.50 (no $ or
   commas)". Checked live: typing `1,234.50` says "line 2 unit price: ...".
8. **Match application was duplicated** in the worker and the review screen;
   now one `apply_match` in the matcher.

### Gates
- Backend **188 passed** (was 181). With the source fixes stashed and the
  tests kept, all 7 new backend tests fail. Frontend **20** unit (was 15),
  `tsc` clean, Playwright 6.
- `creep_report` 93.9% / 0 FP, `matching_report` 0/38709.

## Interaction review: new code vs existing paths (6 findings, all fixed)

1. **Reopen withdrew the disputed alias for one location; the matcher trusts
   the whole business.** A sibling location's copy of the same wrong mapping
   kept auto-matching for the location disputing it. Reopen now withdraws by
   account key, the same scope `match_by_alias` rule 1 uses.
2. **Removing a line kept the aliases it wrote**, and ON DELETE SET NULL then
   erased their provenance. Removing a line says it was never on the invoice,
   so its corrections are withdrawn with it.
3. **The reseed rebuilt every observation but never touched alerts,** so
   Insights showed alerts computed from data that no longer existed.
   `seed_corpus_pipeline` now runs `upsert_creep_alerts` per tenant. Verified
   by planting a stale open alert and reseeding: it came back `resolved`.
4. **The review screen couldn't correct a misread pack size, item code,
   description or unit,** and the arithmetic check never looks at pack size, so
   `4/3 LB` for `4/5 LB` confirmed a per-pound price 67% too high. All four are
   now editable, with three different consequences (`_apply_line_edit`):
   item code or description changed → re-match, and withdraw corrections made
   on the misread code; pack size or unit changed → same product, keep the
   match (even a human-confirmed one) and re-price, or send the line back to
   review if it can no longer be priced; only numbers changed → re-price.
   Checked live: `4/5 LB` → `4/2.5 LB` re-priced $2.375/lb → $4.75/lb and kept
   the match. The editable table also got a layout fix: it was overflowing and
   clipping the Remove column.
5. **The line queue showed lines from invoices with no known distributor,**
   where resolving them wrote no alias and was then overwritten when the
   distributor was chosen. They're now kept out of the queue (and confirm /
   correct refuse them with a reason) until the invoice is attributed.
6. **Queue actions didn't take the invoice lock** the worker and the invoice
   screen take, so a confirm could write an alias under the old distributor
   right after a re-attribution withdrew exactly those. Confirm, correct and
   reopen now lock the invoice and re-read the line under the lock.

### Two test-infrastructure problems this turned up
- **A lock test that hung instead of failing.** Run against the pre-fix code,
  the new lock tests asserted before releasing the session that held the row
  lock, so teardown then blocked on it forever — with the pre-fix code still
  stashed into the working tree. Both lock tests now release in `finally`
  and fail in seconds. (Also: macOS has no `timeout`; use
  `perl -e 'alarm shift; exec @ARGV' N cmd` for a hard limit.)
- **The actual source of the recurring "Test Tenant" / "Other Tenant" debris**
  cleaned up three times this session: `test_skeleton.py` shadowed the
  rollback fixture with a committing one and never cleaned up, leaking two
  tenants and an invoice on every full run. Its fixtures now delete what they
  create; a full run leaves the tenant count unchanged (verified), and the 22
  leaked tenants are gone.

### Gates
- Backend **195 passed** (was 188); with the source fixes stashed, all 8 new
  tests fail. Frontend 20 unit, `tsc` clean, Playwright 6.
- `creep_report` 93.9% / 0 FP, `matching_report` 0/38709.

## Phase 2 validated against the real API

API credit is in place, and Phase 2's gate has finally been measured rather
than assumed. **20-invoice sample (`claude-sonnet-4-6`): PASS on every gate.**

| Gate | Target | Measured |
|---|---|---|
| Line-item accuracy, clean (18) | >= 95% | **100%** |
| Line-item accuracy, noisy (2) | >= 85% | **100%** |
| Arithmetic catch rate | >= 90% | **100%** (1,035/1,035) |
| Mean cost per invoice | < $0.15 | **$0.086** (max $0.142, 114 lines) |

Field accuracy 99.8-100%; review routing 10%. Spend for the whole effort
~$3.20.

### What the first real run found (and would have shipped)
- **Large invoices were truncated.** `MAX_TOKENS = 8000` cut a 114-line
  invoice's JSON off mid-string; the retry hit the same cap; in production
  every large invoice would have been marked `failed`. Output is ~150-180
  tokens per line. The extractor now streams (the SDK requires it for large
  caps) with `max_tokens=64000` (Sonnet 4.6 allows 128K; unused headroom
  isn't billed), and fails fast on `stop_reason` `max_tokens` / `refusal`
  instead of retrying into the same wall and paying twice.
- **The report died on the first failure,** discarding the 12 invoices
  already paid for. A failed extraction is now scored as extracting nothing
  and the run continues; the report prints line count, recall and cost per
  invoice, and writes them to the results file.
- **The catch-rate metric counted corruptions the validator is designed to
  tolerate.** The corpus prints money to four decimals, so ~20% of random
  digit swaps moved a line by under a cent, inside the deliberate one-cent
  rounding tolerance. Measured offline over 15,045 corruptions, every miss
  moved a line by <= $0.01. The metric now counts only corruptions larger
  than the tolerance and reports how many it excluded (301 in this run); the
  0.90 threshold is unchanged. Documented in `thresholds.yaml`.

### Caveats worth carrying forward
- **Noisy scans are under-sampled** (2 of 20). Their weakest field was pack
  size, 85.7% — the one field arithmetic can't check and the one that skews
  price per base unit. The full 200-invoice gate should settle it.
- **Cost scales with line count.** Output tokens dominate; an invoice beyond
  ~120 lines exceeds $0.15 on its own. The gate is on the mean, which has
  room, but real customers' invoice sizes decide the unit economics. Levers if
  needed: Batch API (50%), a leaner output schema, or a cheaper model
  (`claude-sonnet-5`, $2/$10 vs $3/$15; the spec pins `sonnet-4-6`).
- **The corpus PDFs are clean renders,** so this validates the pipeline, not
  accuracy on real photos and faxes.

## Login and the audit trail

v0 had no authentication (SPEC.md §1): every endpoint trusted a `tenant_id`
query parameter, and nothing recorded who changed what. Both mattered once
corrections could travel across businesses: a review-queue correction changes
numbers other businesses read in their benchmarks.

### What was built
- **Email + password sign-in.** Argon2id hashes; server-side sessions stored
  as a SHA-256 of a random token behind an HttpOnly, SameSite=Lax cookie
  (`ii_session`, 14 days), so logout and deactivation take effect
  immediately. Same message and same timing for "no such user" and "wrong
  password". Throttled per address from the audit trail itself (10 failures
  in 15 minutes; a successful sign-in resets the count).
- **Access is enforced in one place.** `get_db_for_tenant` moved from
  `app/db.py` to `app/auth.py` and now requires a signed-in user with access
  to that location, so no endpoint can get a tenant session that skipped the
  check. Another business's location answers 404, not 403. Access is per
  location (`tenant_memberships`); operators (`is_operator`) see every
  location plus Businesses, and `/accounts` and `/tenants` are
  operator-only. Distributors and SKU search need any signed-in user.
- **CSRF:** writes that carry the session cookie must carry
  `X-Requested-With: invoice-intelligence` (and sign-in always must, against
  login CSRF). SameSite=Lax alone wasn't enough: it treats every localhost
  port as the same site.
- **Page images were public.** The `/renders` static mount served every
  invoice's page to anyone with the URL. Replaced by
  `GET /invoices/{id}/pages/{name}`, authorized like the invoice itself,
  filename pattern-checked, `Cache-Control: no-store` (restaurant office PCs
  are shared).
- **Append-only `audit_events`,** written in the same transaction as the
  change, so neither exists without the other. Recorded: upload, email
  intake and extraction (as the system), invoice edits with before/after of
  every changed field and line, confirm, line add/remove (with the removed
  line's content), review-queue confirm/correct/reopen, business regrouping,
  sign-in/out and failures, and user management. `sku_aliases.confirmed_by_user_id`
  (in the schema since Phase 0, never set) is now filled in.
- **Frontend:** sign-in page; middleware sends anyone without a session to
  it and back where they were; location switcher (cookie `ii_location`, only a
  preference, since the API checks every request); Activity page per location;
  History panel on every invoice. The browser talks to the API through a
  Next.js rewrite at `/api`, so the cookie is first-party and CORS stays out
  of it. Sign-out is a form POST to `/logout`, because a click handler did
  nothing if pressed before hydration (Playwright found this).

### Managing users
There's no sign-up page. `backend/scripts/manage_users.py`
(`create`/`grant`/`revoke`/`set-password`/`deactivate`/`list`, run with
`PYTHONPATH=backend`) prompts for passwords and never takes one as an
argument. `make dev-users` makes a local operator and a member of one
location with generated passwords, written to the gitignored
`backend/dev_users.local.json`. `make smoke` and the Playwright suite each
make their own throwaway login per run. The Playwright location is now
`E2E_TENANT_ID` in `frontend/.env.local` (was `NEXT_PUBLIC_DEV_TENANT_ID`).

### Gates
- Backend **216 passed** (was 197): 19 new in `tests/test_auth.py`, run with
  real cookies. Existing API tests run as an injected test operator
  (`conftest.py`), and a run leaves no audit rows or users behind (verified).
- Frontend 25 unit (5 new), `tsc` clean, Playwright **7** (new: sign-in and
  redirect-back, wrong password, member sees no Businesses, sign-out; the
  review test now also checks the invoice's history shows who fixed which
  number, `74.50 -> 47.50`).
- `make smoke` passes on the fake extractor. Against the real API it fails
  because **the Anthropic credit is exhausted again**; the audit trail
  recorded the failure correctly, as the system.
- `creep_report` 0 FP, `matching_report` PASS, `corpus_report` PASS.

### Still open
- `session_cookie_secure` defaults to false for local http; it must be true
  in any deployment.
- Expired sessions are never pruned (harmless, but the table only grows).
- No self-service password reset; an operator resets passwords with
  `manage_users.py set-password`.

## Users screen, session pruning, production guard

- **Users screen (`/users`, operators only).** List everyone with their
  locations and last sign-in; add a login (temporary password generated and
  shown once, `Cache-Control: no-store`); grant or remove a location;
  deactivate (signs them out immediately) or reactivate; make or remove an
  operator; reset a password (also signs them out everywhere). An operator
  can't demote or deactivate themselves, so a slip can't leave no one able
  to undo it. Every change is audited with the operator who made it.
- `app/users.py` is the single implementation behind both the screen
  (`app/api/users.py`) and `scripts/manage_users.py`, which is still how the
  first operator gets created.
- **Dead sessions are pruned** on every sign-in (expired or revoked more than
  `session_retention_days`, 30, ago), indexed by migration 0010; also
  `manage_users.py prune-sessions`.
- **`APP_ENV=production` refuses to start** with insecure session cookies or
  any `http://` frontend origin.
- Test hygiene: the pytest operator is now deleted after each run, and the
  Playwright users test deletes the logins it created.

### Gates
- Backend **225 passed** (was 216; 9 new user-management tests).
- Frontend 25 unit, `tsc` clean, Playwright **8** (new: an operator creates a
  login, the new person signs in with the shown password in their own
  browser, and deactivation signs them out).

### Still open
- No "change my own password" screen; an operator's own row has no actions
  (another operator, or the CLI, resets it).

## Operators' audit log

- **`/audit` (operators only)**: every event across all locations, including
  those tied to none (sign-ins and failures, logins and access, businesses).
  Filter by kind (invoices, review queue, users and access, sign-ins,
  businesses), by person, by location; "Older" pages back. Each entry names
  its location and links to its invoice.
- `GET /audit` pages with a `(occurred_at, id)` cursor, so a page boundary
  can't skip or repeat events that share a timestamp. The action filter is a
  literal prefix (`_` is escaped: `invoice_` doesn't match `invoice.`).
- **Bug found building it:** `occurred_at` defaulted to `now()`, which in
  Postgres is the *transaction's* start time, so events written together (a
  login created and its locations granted) shared one timestamp and listed in
  random order. Now `clock_timestamp()` (migration 0011).
- User-management and sign-in events now read as sentences ("gave … access
  to Cedar Table", "deactivated …"); a failed sign-in is "Someone", not
  "System".

### Gates
- Backend **228 passed** (3 new: operator-only and location-less events,
  filters, paging without gaps or repeats). Frontend 26 unit, `tsc` clean,
  Playwright 8 (the users test now also checks the audit log shows the
  creation and deactivation).

## Production plumbing, part 1: file storage and real inbound email

### Storage (`app/storage.py`)
- One interface, two backends: **local** (a directory; dev and single-server)
  and **s3** (any S3-compatible bucket: AWS, Cloudflare R2, MinIO), set by
  `STORAGE_BACKEND`. Needed once the API and worker don't share a disk.
- Originals are stored at `originals/<invoice>.pdf`, pages at
  `renders/<invoice>/page_NNN.png`. The invoice row keeps a URI, and reads
  follow the URI's scheme, so invoices recorded before a switch still open.
- The worker reads the original from storage, renders into a scratch
  directory, and stores the pages (clearing an earlier attempt's first). The
  review screen and the page endpoint read pages from storage.
- Tests no longer write into the real `backend/uploads`: every test gets its
  own temporary storage. A full upload -> extract -> page-view run is tested
  against a mocked S3 bucket (moto).

### Inbound email webhook (`POST /inbound/email`, via `/api/inbound/email`)
- Takes whatever a provider sends: the raw message, SendGrid's raw form
  field, Mailgun's `body-mime`, or Postmark's `RawEmail`. Authenticated by a
  shared secret (Basic password in the webhook URL, or Bearer); off unless
  `INBOUND_EMAIL_SECRET` is set.
- Same routing as the watch directory: intake was split into
  route -> record -> enqueue, and both entry points use it.
- **Nothing silently dropped:** an email that can't become invoices is kept
  in storage under `inbound/rejected/` and logged as `email.rejected`,
  visible on that location's Activity page when the recipient was known, and
  in the operators' audit log either way. The provider gets a 200 so it
  doesn't retry the impossible.
- **Exactly once:** a retry of an ingested message is a duplicate. A Postgres
  advisory lock on (location, Message-ID) makes the duplicate check hold even
  for simultaneous deliveries; the test for that fails 3/3 with the lock
  disabled.
- Form bodies are parsed with Python's email parser, not Starlette's: newer
  Starlette caps a text field at 1 MB, and SendGrid sends the whole message,
  PDFs included, as one.

### Gates
- Backend **244 passed** (16 new: storage on both backends, S3 end to end,
  webhook formats, auth, rejections, retries, races, size limit).
- Frontend 27 unit, Playwright 8. `make smoke` passes on the fake extractor
  with the new storage path (the real-API smoke is still on hold).

### Adding locations
A real deployment had no way to create a location: tenants only ever came
from the synthetic seed. Operators now add one on the Businesses page (name,
metro picked from the existing ones where it fits, since benchmarks compare
within a metro, and annual food-spend band). Each gets a unique invoice
forwarding address, numbered if another location has the same name, shown
on creation and in an "All locations" table. `POST /tenants`, operator-only,
audited as `tenant.created`. Backend 246 passed.

## Production plumbing, part 2: deployment

`deploy/` runs the whole service on one Docker host: Caddy (automatic
HTTPS, security headers) -> Next.js -> API, plus worker, Postgres/pgvector
and Redis. Only 80/443 are published; the API, including the inbound email
webhook, is reached through the frontend's `/api` proxy. A `migrate` step
runs migrations and the SKU catalog seed before the API starts, on every
deploy. `deploy/README.md` covers DNS, first operator, onboarding a
restaurant, mail-provider setup per provider, storage, backups and updates.
Images: backend 2.2 GB (CPU-only torch; the default PyPI wheel drags in
gigabytes of CUDA), embedding model baked in and run offline; frontend
224 MB (Next standalone).

### Verified by actually running it
The full stack was built and run locally over HTTPS (`localhost:8443`, a
generated throwaway env): first operator created with `manage_users.py`
inside the container, sign-in (cookie `Secure; HttpOnly; SameSite=lax`),
location created, a PDF uploaded and an email posted to the webhook with
provider-style Basic auth, both extracted by the worker (fake extractor, no
API key), a retried email a duplicate, a wrong secret 401, page images 200
signed in and 401 signed out, HSTS/nosniff/DENY headers present. Torn down
afterwards; nothing of it is left on disk.

### What running it found
- **Migrations could not build a database from empty.** 0001 called
  `create_all()` on the *current* models, so on an empty database step 1
  created everything and 0004 then failed adding `tenants.inbox_address`.
  The dev database had only ever been migrated forward. 0001 now writes out
  the first schema explicitly, regenerated from the models at the commit the
  first databases were built from (a91a168); a database migrated from empty
  is now byte-identical (`pg_dump -s`) to the dev database and has zero drift
  from the models. `tests/test_migrations.py` builds a throwaway database
  from empty on every run, plus a full downgrade/upgrade round trip; it fails
  on the old 0001 with the exact production error.
- **Sign-out would have failed in production.** `/logout` compared the
  request's Origin with its own URL, but behind the HTTPS proxy Next sees
  `http://`, so every real sign-out was refused as cross-origin. It now
  compares hosts and redirects using the forwarded scheme; verified 303 to
  `https://.../login`, and a cross-site POST still 403.
- `validation/extraction_report.py` still passed a `file://` path to the
  renderer (now bytes), and its `--fake` mode applied accuracy thresholds to
  a fake extractor whose output is fixed by design, so a dry run could never
  pass; it now says it's a dry run and skips them.
- `requirements.txt` is runtime only (torch and transformers now pinned to
  what dev runs); test tools moved to `requirements-dev.txt`.

### Gates
Backend **248 passed** (2 new migration tests), frontend 27 unit, `tsc`
clean, Playwright 8.

## Calibrating on real invoices

The thresholds were all tuned on the synthetic corpus. Two tools for tuning
them on real invoices, neither of which needed (or spent) API credit to
build; both are ready for real data.

### From real use: `make calibration-report`
People using the app are already producing labels. `validation/calibration_report.py`
reads them from the audit trail:
- **Matching:** every review-queue confirm or correct is a verdict on a
  suggestion made at a known confidence (now recorded with each verdict as
  `suggested_confidence`, and on a reopen as `match_confidence`). Reported
  per confidence band, with how often auto-accepted matches were later
  disputed.
- **Extraction:** on invoices a person reviewed and confirmed, which fields
  they had to correct (a field fixed twice on one line counts once), lines
  added (missed) and removed (invented), and holds where nothing needed
  fixing (false alarms).
- **Suggestions only on evidence.** A threshold move is suggested only when
  the 95% Wilson interval supports it against the 1% false-match ceiling:
  with zero errors that takes 381 decisions, and a band with 50/50 correct
  moves nothing. It exits non-zero if disputed auto-accepted matches show the
  auto threshold is too loose, the one finding that means wrong prices reach
  benchmarks now.

### From a labelled set: `make real-invoice-report DIR=...`
`validation/real_invoice_report.py`: `--extract` renders and extracts each
PDF once (cached beside it with its cost; `--limit` caps a run's spend) and
writes a truth template from the extraction for a person to correct and mark
`"_reviewed": true`. Scoring is free and repeatable: line recall/precision,
per-field accuracy, cost, and what synthetic data can't show, whether the
arithmetic check catches real mistakes and how often it holds correct
invoices anyway. A reviewer's edits are never overwritten by re-extraction.
`--fake` is a free dry run with its own cache files.

### Also in this pass
- Audit log: an "Older" page in flight when the filters change is dropped,
  not appended; activity detail lines keyed by position.

### Gates
Backend **257 passed** (9 new: Wilson bounds incl. the 381 figure, banding,
evidence-gated suggestions, extraction counting, verdict confidence recorded
via the API, harness extract-once/reviewed-only/mistake detection). Frontend
27 unit, Playwright 8.

### To actually calibrate
Needs what can't be built: real invoices and API credit. Gather 20-50 real
PDFs, `make real-invoice-report DIR=... ARGS="--extract --limit 20"` (roughly
$0.10 each at the Phase 2 measurement), correct the truth files, score. And
once restaurants are reviewing, `make calibration-report` periodically.

## Code review of this round (10 findings) and interaction bugs (6), all fixed

### Review findings
1. **Uploads couldn't be read back from a path with a space.** `LocalStorage`
   writes `Path.as_uri()` (percent-encoded); `read_uri` didn't decode it.
   Reproduced, fixed, tested with a spaced and non-ASCII path.
2. **Large invoices would time out.** Jobs were queued with RQ's default
   180 s limit; a big invoice streams for minutes (twice with the retry).
   Both entry points now go through `enqueue_extraction` with
   `EXTRACTION_JOB_TIMEOUT_SECONDS` (1800); verified on the live queue.
3. **The guide's backup command produced corrupt dumps** (`compose exec`
   without `-T` puts the binary dump through a terminal). Fixed, with a
   restore command; both verified by backing up the dev database and
   restoring it into a scratch one (39,331 invoice lines back).
4. **Emails without a Message-ID were never deduplicated,** so a provider's
   retry doubled the invoices. They now dedupe on a hash of the raw message.
5. Businesses page: the Add location and Create business forms cleared
   what was typed even when the API refused it.
6. **Orphaned files:** originals were stored before the row committed; a
   failed write now removes them (upload and email). Test fails without it.
7. **Two locations with one name created at once** hit the inbox-address
   unique index and 500'd; now retried with the next number (test fails 3/3
   without the retry).
8. `TenantSummary.from_tenant` replaces two hand-built copies.
9. Dev, smoke and e2e logins go through `app/users.py`, so their resets and
   grants are audited like everyone else's.
10. `real_invoice_report` writes results with `default=str`.

### Interaction bugs (this round's code against what was there)
- **Switching storage to S3 would have blanked every existing invoice's page
  images:** originals follow their stored URI, but pages are looked up in the
  current backend only, and the guide said the opposite. New
  `scripts/migrate_storage.py` (dry run, re-runnable, deletes nothing) moves
  originals and pages; the guide now says to run it. Tested against a mocked
  bucket.
- **A new location's metro could split a benchmark cell:** "austin, tx" and
  "Austin, TX" are different cells to benchmarks and observations. A metro
  matching an existing one (case, spacing) takes the existing spelling.
- **A fresh deployment's first operator was told to "ask whoever set up your
  login"** on every page, having no locations yet. Operators are now pointed
  at Businesses and Users.
- **Calibration couldn't classify verdicts on unscored suggestions:**
  `audit.record` drops `None`, which looked the same as a pre-release event.
  Recorded as `"none"` now.
- The location cookie is set `Secure` over https.
- `tenant.created` reads as a sentence in Activity.

### Gates
Backend **266 passed** (9 new), frontend 28 unit, `tsc` clean, Playwright 8,
`make smoke` on the fake extractor with the job carrying the 1800 s limit.

## Times in the reader's zone, indexed audit lookups, browser tests for operators

- **Activity and History showed server time.** They were formatted where
  the page renders: the laptop's zone in development, UTC in the production
  container, so a restaurant would have seen an 8:40 AM correction as
  3:40 PM (and the Audit log, formatted in the browser, would disagree with
  both). `LocalTime` formats in the browser, after hydration; the server's
  first render says "UTC" explicitly. Playwright test runs the browser in
  Asia/Tokyo against a server on EDT: the old code showed "Sep 27, 2:01 PM"
  where "Sep 28, 3:01 AM" was right.
- **Audit lookups inside `details` indexed** (migration 0012): an invoice's
  history by `details->>'invoice_id'`, sign-in throttling by
  `details->>'email'`. Measured on a scratch database with 300k events for
  one location: history went from a sequential scan of the whole log (21 ms,
  growing with every event) to an index lookup (0.04 ms). That needed the
  query to state the partial index's condition (`details ? 'invoice_id'`);
  without it Postgres ignored the index, so the first version of this fix
  would have changed nothing. Throttling under 50k failed sign-ins: 0.07 ms
  on its index.
- **Browser tests for the operators' screens:** adding a location (the
  forwarding address shown, the row listed, and a refused create keeping
  the typed name), and audit-log paging (pages of two, walked to the end,
  nothing missing or repeated).

### Gates
Backend 266 passed, frontend 28 unit, `tsc` clean, Playwright **11**
(3 new). Test data cleaned up after the run (verified).

## Sessions that stay alive while used; changing your own password

### Sliding sessions
A session now ends 14 days after it was last *used* (it was 14 days after
sign-in, so everyone was signed out mid-task every two weeks), capped at 90
days after sign-in so a stolen cookie can't be kept alive by using it.
Extended server-side on use, at most one write per session per hour. The
cookie itself lives to the cap and the server decides validity: most pages
render on the server, whose responses to its own API calls never reach the
browser, so refreshing the cookie couldn't have worked.

### Changing your password
- **Account page** (`/account/password`, from your name in the nav): needs
  the current password (a session left open on a shared office PC mustn't be
  enough), and wrong guesses count toward the sign-in lockout. Your other
  sessions end; this one stays.
- **Operator-issued passwords must be replaced.** A login created or reset on
  the Users screen has a password the operator has seen; until its owner
  chooses their own, the API refuses everything except `/auth/me`,
  `/auth/password` and `/auth/logout` (enforced in `current_user`, so no
  endpoint can be reached around it), and every screen sends them to the
  change page. Command-line logins, where the person types their own, aren't
  flagged. Migration 0013.

### Found while testing it
**Forms could be used before the page was interactive.** Playwright filled
the change-password form before React had loaded: on submit the browser did
its own GET to the same URL (the page just reset), and once React took over
it wiped the typed values, so the next submit was silently blocked by the
browser's "required" check. Real people on a slow phone would hit the same.
The sign-in and change-password forms are now disabled, fields included,
until the page is interactive (`useHydrated`). (The sign-out button had the
same race earlier and became a plain form POST.) The inputs have no `name`
attributes, so no password ever went into a URL.

### Gates
Backend **275 passed** (9 new: sliding expiry, the cap, write throttling,
cookie lifetime, forced change, keep-this-session, lockout on wrong current
password, length and reuse). Frontend 28 unit, `tsc` clean, Playwright 11
(the users test now walks the forced change end to end).

## Continuous integration

`.github/workflows/ci.yml`, on every push to main and every pull request,
on fresh services (nothing depends on a developer's database; no API key is
set, so the real Anthropic API is never called):
- **backend**: migrate from an empty database, seed the catalog, generate
  the synthetic corpus, the full test suite, the corpus / matching / creep
  gates, an extraction-report dry run, and a validity check of the
  production compose file.
- **frontend**: type check, unit tests, production build.
- **e2e**: the same setup plus analytics seeding, the API and worker
  started, and the whole Playwright suite; logs and traces kept on failure.

Rehearsed locally before pushing, exactly as CI will run it: a fresh
database with only migrations and the catalog passes all 275 backend tests;
the corpus regenerates byte-identical (it's deterministic); the gates pass;
the Playwright suite passes all 11 against a second API and dashboard on
that fresh database; the production build compiles. Setup takes about a
minute (corpus 25 s, analytics 30 s). `e2e_fixture.py pick-tenant` chooses
the location CI tests in; `E2E_PORT` lets a second Playwright run sit beside
a dev server.

## Review of sessions, passwords and CI (9 findings, all fixed)

1. **A password change didn't lock out a copied cookie.** Other sessions
   ended, but this browser kept its token, so someone holding a copy of it
   (the shared-PC case the current-password check exists for) stayed in. The
   current session now gets a new token on the change.
2. **The must-change exemption matched URL text.** Served under a prefix,
   the ASGI path carries it (`/api/auth/me`), so a flagged user would have
   been refused even the change endpoint, permanently. Exemption is now by
   dependency: `signed_in_user` for who-am-I and the change, `current_user`
   (enforcing) for everything else. The test fails on the old code (403).
3. **Command-line passwords weren't must-change,** though the person at the
   terminal knows them just as an operator on the Users screen does. Now
   they are; `--permanent` for your own (the first operator, per the guide).
4. A wrong current password during a change was logged as an anonymous
   failed sign-in. Now `auth.password_change_failed`, attributed to the
   user, and still counted toward the lockout.
5. CI no longer cancels a run on `main` when a newer commit lands (a
   cancelled run could hide a failure); only superseded PR runs.
6. CI's Playwright suite now runs against the production build served as the
   Docker image serves it, not `next dev` (cold per-page compiles, and not
   what users get). Rehearsed locally: 11/11.
7. The change-password path and the API's "password change required" detail
   are defined once (`lib/api.ts`), not in four files.
8. Password events read as sentences in Activity and the Audit log.
9. CI now runs `smoke.py` against the running API and worker: an upload
   queued, extracted by the worker (fake extractor), the one path nothing
   else in CI exercised.

Backend **279 passed**, frontend 29 unit, Playwright 11.

## The weekly email digest

Alerts and invoices that need a look only helped if someone opened the
dashboard. Now each person gets one email a week (Monday 12:00 UTC) with a
section per location they belong to: new price increases (with units), the
invoices that couldn't be read or don't add up, lines waiting in the review
queue, what arrived and its total, and the three biggest savings on the
negotiation sheet. A location with a quiet week is left out; if all of
someone's locations were quiet, they get nothing. Links carry `?location=`,
which the dashboard's middleware turns into the selected location, so a
link from one location's section never opens in another's.

- **Recipients:** active users with at least one location who haven't
  turned it off. Operators only for locations they're members of.
- **Turning it off:** a toggle on the account page, or the signed
  unsubscribe link in every digest. The link shows a confirmation page,
  because mail security scanners open every link and a GET that acted would
  unsubscribe people who never clicked; POST (the button, or a mail client's
  own one-click unsubscribe per RFC 8058's List-Unsubscribe-Post) does it.
  Forged links (another person's id with your signature) do nothing.
- **Sending:** SMTP (every major provider has a relay), or in development
  an `outbox` folder of `.eml` files. `make digest-preview EMAIL=...` writes
  one person's digest there without sending or recording.
- **Idempotent:** each (person, week) is claimed in `digest_sends` under a
  unique constraint before sending; a failed send releases its claim and is
  retried on the scheduler's next check. The deployment's `scheduler`
  service checks every five minutes, so restarts and redeploys are safe and
  there is no missed window.
- **Production guard:** refuses to start without a `SECRET_KEY` (it signs
  the links), an https `PUBLIC_BASE_URL`, and SMTP, unless
  `DIGESTS_ENABLED=false`.

### Found building it
- The account page toggle only moved when the server answered, so for that
  moment a click looked ignored (Playwright's `uncheck` noticed). It now
  moves at once and reverts if the save fails.
- Test logins accumulated state between runs: the e2e member kept every
  location it had ever been given (a run against CI's location left it with
  two, and the dashboard then opened on the wrong one), and a run stopped
  mid-test could leave it unsubscribed. The fixture now gives it exactly one
  location, and resets preferences with the password. Verified by running
  against CI's location and then the local one back to back.

### Gates
Backend **294 passed** (15 new), frontend 29 unit, `tsc` clean, Playwright
**13** (2 new: a digest link opening on its location; the account toggle).

## A green theme, color-coded numbers, and actions that stand out

- **Palette:** a light green accent (`brand` in `tailwind.config.ts`): a
  green band across the top of every page, the current section highlighted
  in the nav, green focus rings, links and card accents. Fills use the light
  end with dark green text on them (about 9:1 contrast), so it stays readable.
- **One set of controls** (`globals.css`): `btn-primary` (bold light green)
  for the thing to do next on each screen (Upload invoice PDF, Confirm
  invoice, Confirm match, Print, Add user, Create login, Add location, Sign
  in), `btn-secondary` for the rest, `btn-danger` for Deactivate. Cards,
  inputs, badges and table headers share one look.
- **Numbers mean something by color** (`lib/format.ts`, tested): price
  increases red, decreases and savings green; statuses as badges (done
  green, needs a person amber, broken red, in progress blue); match
  confidence green / amber / red against the review band. Numbers are also
  *formatted* now: they were printed raw from the database ("4240.1143",
  "1250.0000"); totals are dollars and cents with separators, unit prices
  keep sub-cent precision only below a dollar, quantities drop padding zeros.
  Tabular figures, so columns line up.
- **Per screen:** stat cards on Invoices; red-accented alert cards with a
  red ▲ percentage and red sparkline on Insights; a green "savings on the
  table" banner and red-you-pay / green-target columns on the negotiation
  sheet; a progress bar and a Confirm-match button on the review queue;
  each price on a SKU's page colored against the delivery before it;
  activity events marked by kind (problems red, invoices blue, matching
  green, access amber, sign-ins gray).
- **The weekly digest** matches: green header band and mark, red increase
  pills, a green savings panel with its total, and real buttons.
- **Layout:** the header is one row at 1280px and up, every section link
  visible down to 1024px (the account menu takes a second row), and a
  sideways-scrolling link row on phones; it's sticky only on large
  screens, because on a phone it covered a third of the page. Tables scroll
  on narrow screens instead of wrapping dates onto three lines. Measured with
  Playwright at 1280 / 1024 / 390px.
- The review-queue e2e test found its item by CSS classes; it now uses
  test ids, so styling can change without breaking it.

### Gates
Backend 294, frontend 35 unit (6 new), `tsc` clean, Playwright 13.

## Visual audit of the new theme: spills, overflow and alignment

Every page was measured, not eyeballed, at 1440 / 1280 / 1024 / 768 /
390px with a Playwright script: horizontal page scroll, anything past the
viewport (outside deliberate scroll areas), text wider than its own box, and
anything poking out of its card, table cell, button, badge or list row.
Stress data: a location and a person with very long names, a 70-character
product description, a 60-character word with no spaces, a $1.2M line, an
invoice that doesn't add up, and an empty failed invoice (all removed after).
Screenshots of each page and width were then reviewed for alignment.

First pass: every page clean from 768px up; **169 findings at phone width**.
Fixed, to zero:
- **Header** overflowed every phone page by 7px (location picker + name +
  sign out); the account row now takes its own line with a picker that
  shrinks, and the current section scrolls into view in the link row.
- **Invoice review** was 248px wider than a phone: a fixed 288px page-image
  column sat beside the table even when there was no image. It's now shown
  only when there's an image, beside the table from 1280px up (it also
  clipped the Match and Remove columns at 1024px). An invoice with no lines
  shows one empty-state box, not a header row over nothing.
- **Audit log filters** ran 280px past a phone screen: a select is as wide
  as its longest option, and names can be long. Capped; same for the Users
  page's location picker.
- **Long addresses** (emails, forwarding addresses) with no spaces ran off
  the activity feed; they break anywhere now, and stay on one line inside
  scrolling tables.
- **Wrapping mid-value:** dates broke onto three lines ("2026- / 08- /
  24"), a price's ▲ split from its percentage, table headers wrapped
  ("PRICE / LB"), a "needs review" badge became two pills, the negotiation
  basis buttons wrapped their labels. Values, headers and badges no longer
  wrap; tables scroll instead, and name columns keep a readable minimum.
- **Users at 768px:** the action buttons pushed Deactivate off the card;
  they wrap now.
- **Edit boxes** showed raw stored decimals ("2.0000", "74.5000"); they
  start as "2", "74.50" and keep sub-cent places only when meaningful
  ("0.6656"). Edits are still compared as numbers, so nothing reads as a
  change that isn't.

**Digest at phone width (375px):** each location's box ran 10px past the
edge (a 100%-wide table plus its border), the ▲ split from its percentage,
and whole prices crushed product names to three lines. Fixed; measured
clean at 320 / 375 / 600px.

**One race, found by the test suite:** the optimistic email toggle moved at
once, so a reload right after could beat the save; the save is sent with
`keepalive` (it lands even if the page is left) and the test waits for it.

### Gates
Audit 0 findings across all pages and widths; backend 294, frontend 36
unit, `tsc` clean, Playwright 13 (twice in a row).

## Photo upload, CSV export, price-increase emails, forgot password

### Phone photos of paper invoices
Upload took PDFs only, and most restaurant invoices are paper. "Upload
invoice" now takes a PDF (several at once, one invoice each) or photos; on
a phone, "Take photo" opens the camera. Photos are gathered as pages in a
tray (thumbnails, remove, add another page, up to 10) and sent together as
one invoice. The server turns them into a PDF (`app/ingest/photos.py`),
so storage, rendering, the review screen and extraction are unchanged, and
the invoice's source is `photo`.
- JPEG, PNG, WebP and HEIC (iPhone's default; `pillow-heif`), recognized
  from the bytes, never the name.
- Turned upright from the EXIF flag, flattened onto white, scaled to 3,000px,
  placed on a letter-sized page (a phone photo's missing DPI otherwise makes
  a 56-inch page). Each page keeps its own size; Pillow's multi-page save
  gave every page the first one's.
- Refused before anything is stored, with a reason: a PDF mixed with photos,
  more than 10 photos, an unreadable photo, over 50 MP decoded (a small PNG
  can decode to hundreds of MB in the API process; big JPEGs decode at
  reduced size instead), over the upload limit counting every file.

### CSV export
"Export CSV" on Invoices: invoices or line items, a period (this/last month
or quarter, year to date, last year, all, or dates), and a distributor
(`GET /exports/{invoices|line-items}`). Built for Excel: byte-order mark
(accented descriptions), CRLF, amounts to the cent, unit prices at their
billed precision ($0.5432/lb). Every invoice is included with its status,
since unreviewed invoices are still money spent. Supplier text that would
run as a formula ("=HYPERLINK(...)") is defused; numbers aren't, so a
credit stays -12.50 rather than text. Each export is in the audit trail.

### Price-increase emails
The digest reported increases on Monday. Now an alert of 10% or more
(`ALERT_EMAIL_MIN_PCT_CHANGE`) is emailed within five minutes (the
scheduler service) to members of that location who want it: one email per
person per run, covering every new alert across their locations.
- Idempotent per (alert, person) under a unique constraint; a failed send
  is released and retried.
- Only alerts opened in the last 48 hours, so deploying this (or an outage)
  doesn't email every alert ever raised.
- A product already emailed to someone at a location isn't emailed again
  for a week, even if its alert closed and reopened.
- Turned off separately from the digest, on the account page or by the
  email's own unsubscribe link (the same signed link as the digest's, per
  kind; old digest links still work).
- The digest's look moved to `app/email_design.py`, shared by all three
  emails, and it gained a viewport tag: phone mail apps were laying the
  emails out at desktop width and shrinking them.

### Forgot your password
"Forgot your password?" on sign-in emails a single-use link, valid for an
hour, at most three an hour per address. The answer and the page are the
same whether or not the address has an account, and the email goes out
after the response, so timing doesn't tell either. The link signs you in
and ends every other session; it clears an operator-issued temporary
password and a sign-in lockout. Only a hash of each token is stored. The
token leaves the address bar as soon as the page has it (kept for the tab
only, so a reload still works) and the page sends no referrer. Changing or
resetting a password any other way ends outstanding links.

### Production settings
Production refuses to start with any of the three emails on and no SMTP
relay, since outbox mail there is never delivered; each has its own switch
(`DIGESTS_ENABLED`, `ALERT_EMAILS_ENABLED`, `PASSWORD_RESET_ENABLED`).
Migrations 0015 (alert email sends, per-user switch) and 0016 (reset links).

### Review, before commit
- A credit's "-5.00" in the export would have been quoted as text by the
  formula guard; the guard now applies to text fields only.
- A price wobbling around the threshold would have emailed the same product
  each time its alert reopened; now once a week at most.
- Photos: huge PNGs decoded in full (memory); oversized images raised
  Pillow's own error as a 500. Both now refused with a reason.
- The client-side session check sent the new signed-out pages to sign-in
  (only /login was exempt); one list of public pages now serves both it and
  the middleware.
- The alert tests picked up the dev database's own recent alerts and
  recorded sends for a dev user; the tests now run at a date no real data
  reaches, and the rows were removed.
- Reset links are pruned like sessions; a dead one is cleared from the tab.

### Gates
Backend 325 (31 new), frontend 48 unit, `tsc` and `next build` (with lint)
clean, Playwright 19 (6 new). The new screens were checked at desktop and
375px, and the two new emails at 375px.

## Bug check of photo upload, export, price-increase emails and password reset

Ten findings, all fixed.

- **Price-increase emails would never have been sent.** The scheduler's
  session expires what it loaded at every commit. After recording a send,
  the sender re-read the alert, a location-scoped query on a session with no
  location, which the tenant guard refuses. The crash left the alert
  recorded as sent. Reproduced against a real session. The sender now works
  from plain snapshots taken before any commit. The tests missed it because
  their session never expires anything; they now send the way the scheduler
  does, and the new test fails on the old code. The digest got the same
  test and passes.
- **Any failure after recording a send now undoes it**, not only a refused
  send, so the next run retries.
- **Excel export.** Opened from a CSV, Excel dropped leading zeros ("0081234"
  became 81234) and turned 16-digit codes into 1.23457E+15, and no CSV can
  prevent it. The export now defaults to a real .xlsx: codes and invoice
  numbers are text, amounts and dates are numbers and dates, supplier text
  is never a formula, and characters a workbook can't hold are dropped
  rather than failing the export. CSV stays, for accounting-software imports.
- **Password reset requests** now do all their work after the response,
  which is then the same speed whether or not the address has an account.
  The hourly limit is counted under a per-address lock: 8 simultaneous
  requests used to all get through, and now exactly the limit do (the test
  fails without the lock). Old links are pruned only when a link is
  actually issued, not on every anonymous request.
- The two emails' increase tables are one helper; the two password forms
  share one set of new-password fields and one length rule.
- The e2e password-reset login and its history are removed after each run.

### Gates
Backend 329, frontend 48 unit, `tsc` and `next build` clean, Playwright 20;
no test data left in the dev database.

## App experience pass: speed, buttons, navigation, plain language, help

### Speed
Measured, not guessed, on a production build: every page as a member, an
admin and signed out, at 1280 and 390px. First byte 6-119 ms, fully loaded
36-198 ms; every API call the screens make answers in 3-62 ms (the Excel
line-item export, a download, ~375 ms). Nothing needed speeding up.

A global loading screen was tried and dropped: it made the invoice screen
fall back to stale data after saving (the browser tests caught it).

### Buttons and links
An audit script followed every internal link each kind of user is shown
(all 200), and checked every page for console errors, failed requests,
missing headings, dead anchors and sideways overflow at phone width: 0
problems. The browser suite covers the working buttons end to end (22
tests). Fixed along the way:
- Match items had no way past an item you can't match: **Skip for now**.
- Removing a location from a business happened on one click; it now asks.
- An invoice being read never updated until reloaded; the list and the
  invoice now refresh themselves every 5 seconds until it's done.

### Navigation
- Plain section names, in the order of the work: Invoices, Match items,
  Price alerts, Savings, Products, Activity; for admins Businesses, People,
  Change log. Each page says in one line what it's for, and every page has
  a browser-tab title.
- Help and Your account at the top of every page (Account on a phone, where
  a name squeezed the location picker).
- Back links on invoice and product pages; items to match link to their
  invoice; alert and savings rows link to the product; product history rows
  link to the invoice; a finished queue offers Price alerts and Savings next.
- Invoice links in the change log open at the invoice's own location.
- A proper "Page not found" with a way back.
- Each location's invoice email is shown to its members (Invoices page,
  Your account, Help); only admins could see it before.

### Plain language
Everywhere a person reads: every page, button, empty state, status and
message, the server's error messages, the history log, and the emails.
"Canonical SKU" is a product, "needs_review" is "Needs a look", "extended
price" is the line total, "negotiation sheet" is Savings, "operator" is
admin, "benchmark"/"peer p25" is what similar businesses pay. Server errors
are short sentences ("That email and password don't match.").

### Help
`/help`, open to everyone (linked from sign-in too): getting started in
three steps, then how to add invoices, fix one that needs a look, match
items, read price alerts and savings, look up a product, export for the
accountant, the emails, your account and password, what each label means,
and a section for admins. Every topic links to where it's done; signed in,
it shows your own invoice email addresses.

### Gates
Backend 329, frontend 48 unit, `tsc` and `next build` clean, Playwright 22
(2 new: help links all resolve and it's open signed out; skipping items).

## Spending, the phone app, photos by email, first-time setup, monitoring

### Spending (`/spending`, second in the menu)
What a location spends each month, before tax: tiles for this month, last
month (with the change) and the average month; a bar per month (hover for
the figures, click one to see its breakdown); and where the money went, by
category and by distributor, with each one's share and change against the
period before. Periods: this month, last month, last 3 or 12 months, or any
month clicked. Built from the invoice items, so the breakdowns add up to the
total exactly; invoices that need a look aren't counted (and the page says
how many). Starts at the first month with invoices, at most a year back,
empty months kept. One colour for bars (validated for contrast), labels on
every row, a screen-reader table behind the chart.

### On a phone's home screen
A web app manifest and icons: added to the home screen it opens full screen
straight to Invoices, where Take photo is. On a phone, the Invoices page
offers it once (a button on Android; on an iPhone, where only Safari's Share
menu can do it, the two taps to make); Help explains both. The icons are
served signed out (a phone fetches them without the session), and the
production image and CI now copy `public/`, which a standalone build leaves
out.

### Photos by email
An email with no PDF but photos becomes one invoice, a page per photo (up to
10), same as photos uploaded. Logos and signature pictures aren't pages:
pictures embedded in the message body, files under 30 KB, and anything under
600 px on its short side are left out. A PDF wins when an email has both.

### Get set up
A checklist at the top of Invoices for a new location: add the first
invoice, have invoices emailed in (with the address and a copy button),
match your items, add your team. Each ticks itself off from what has
actually happened (`GET /setup`); the card disappears when all four are
done, or when hidden.

### Monitoring and backups
- `OPS_EMAIL` gets an email, at most hourly per kind of problem (the
  throttle is in Redis, shared by every process), when: the API errors, a
  page breaks in someone's browser, invoices stop being read for a reason
  that isn't the invoice (API key, credit, model service, storage), the
  weekly or price-increase emails can't send, the scheduler fails, or the
  newest backup is over a day old. Without it, they're logged.
- People get plain messages instead: "Something went wrong on our side",
  and a "Something went wrong" page with Try again.
- `/health/ready` for an uptime monitor: 200 when the API, database and
  queue all work, 503 naming which doesn't.
- A `backup` service dumps the database nightly, checks each dump reads
  back, keeps two weeks, and takes the first one at once. With S3 storage
  the scheduler copies new ones to the bucket (the newest 30). Tried for
  real against the development database: a 5 MB dump that restores.

### Along the way
- The scheduler's offsite copy would have re-uploaded the oldest backups
  every hour whenever more were kept locally than off the server; it now
  copies only the newest that it keeps.

### Gates
Backend 348 (19 new), frontend 54 unit, `tsc` and `next build` clean,
Playwright 25 (3 new). The page audit on a production build: every page
under 0.25 s, 0 problems (links, console errors, overflow at phone width).
