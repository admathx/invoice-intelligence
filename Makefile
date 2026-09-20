.PHONY: up down migrate seed seed-analytics smoke watch-inbox test test-e2e validate extraction-report frontend-install frontend-dev

VENV := .venv/bin
RUN_DIR := .run
# Docker Desktop's CLI isn't on PATH on this machine (no Homebrew symlink step was run).
# Point at it directly instead of requiring a PATH/shell-profile change.
DOCKER := /Applications/Docker.app/Contents/Resources/bin/docker

up:
	$(DOCKER) compose up -d
	@echo "Waiting for postgres..."
	@until $(DOCKER) compose exec -T postgres pg_isready -U invoice > /dev/null 2>&1; do sleep 1; done
	mkdir -p $(RUN_DIR) logs
	backend/$(VENV)/uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port 8000 > logs/api.log 2>&1 & echo $$! > $(RUN_DIR)/api.pid
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/run_worker.py > logs/worker.log 2>&1 & echo $$! > $(RUN_DIR)/worker.pid
	@sleep 1
	@echo "API on http://localhost:8000, worker running. Logs in ./logs/"

down:
	-kill `cat $(RUN_DIR)/api.pid` 2>/dev/null
	-kill `cat $(RUN_DIR)/worker.pid` 2>/dev/null
	rm -rf $(RUN_DIR)
	$(DOCKER) compose down

migrate:
	cd backend && $(VENV)/alembic upgrade head

seed:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/seed_catalog.py
	PYTHONPATH=backend backend/$(VENV)/python -m synthetic.generate

# Runs the full corpus through Invoice/InvoiceLineItem creation + the real
# matcher (no Anthropic spend — ground truth stands in for extraction output),
# populating price_observations so Phase 4's analytics have real data. Not
# folded into `make seed`: it's Phase-4-specific and takes ~15-20s, not
# needed for Phase 0-3 work.
seed-analytics:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/seed_corpus_pipeline.py

smoke:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/smoke.py

# Polls inbox/ for .eml files and turns their PDF attachments into invoices
# (SPEC.md §10 Phase 6). ONCE=1 scans what's there and exits.
watch-inbox:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/watch_inbox.py $(if $(ONCE),--once,)

test:
	cd backend && $(VENV)/pytest -q

# Playwright drives its own Next.js dev server (frontend/playwright.config.ts,
# reuseExistingServer: true) but assumes `make up` is already running for the
# backend/worker/postgres/redis it talks to — same assumption every other
# gate above makes.
test-e2e:
	cd frontend && npx playwright test

validate:
	@echo "--- Phase 0 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_skeleton.py
	$(MAKE) smoke
	@echo "--- Phase 1 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_synthetic.py
	PYTHONPATH=backend backend/$(VENV)/python -m validation.corpus_report
	@echo "--- Phase 2 gate (pytest only — extraction-report costs real API spend, run it separately) ---"
	cd backend && $(VENV)/pytest -q tests/test_extract.py
	@echo "--- Phase 3 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_pack_size.py tests/test_alias.py
	PYTHONPATH=backend backend/$(VENV)/python -m validation.matching_report
	@echo "--- Phase 4 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_suppression.py tests/test_negotiation.py tests/test_price_creep.py
	$(MAKE) seed-analytics
	PYTHONPATH=backend backend/$(VENV)/python -m validation.creep_report
	@echo "--- Phase 5 gate ---"
	cd frontend && npm run test
	$(MAKE) test-e2e
	@echo "--- Phase 6 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_email_intake.py

# Costs real money once ANTHROPIC_API_KEY is configured with credit — not part
# of `make validate`. Defaults to a small sample; override with SAMPLE=200 to
# match SPEC.md's Phase 2 gate exactly. Add FAKE=1 for a free dry run of the
# reporting pipeline's mechanics (accuracy numbers from that are meaningless).
SAMPLE ?= 20
extraction-report:
	PYTHONPATH=backend backend/$(VENV)/python -m validation.extraction_report --sample $(SAMPLE) $(if $(FAKE),--fake,)

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev
