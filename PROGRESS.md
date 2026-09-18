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

Status: not started.

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
