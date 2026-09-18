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

## Phase 2 — Real extraction

Status: not started.

## Phase 3 — Normalization

Status: not started.

## Phase 4 — Analytics

Status: not started.

## Phase 5 — Dashboard

Status: not started.

## Phase 6 — Email intake

Status: not started.
