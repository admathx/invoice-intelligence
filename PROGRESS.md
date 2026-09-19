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
  this machine. Backend CORS uses `allow_origin_regex` for `http://localhost:\d+` rather than
  a fixed origin, so the dev port doesn't need to stay in sync with a hardcoded allowlist.
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

Status: not started.

## Phase 5 — Dashboard

Status: not started.

## Phase 6 — Email intake

Status: not started.
