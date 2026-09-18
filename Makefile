.PHONY: up down migrate seed smoke test validate extraction-report frontend-install frontend-dev

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

smoke:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/smoke.py

test:
	cd backend && $(VENV)/pytest -q

validate:
	@echo "--- Phase 0 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_skeleton.py
	$(MAKE) smoke
	@echo "--- Phase 1 gate ---"
	cd backend && $(VENV)/pytest -q tests/test_synthetic.py
	PYTHONPATH=backend backend/$(VENV)/python -m validation.corpus_report
	@echo "--- Phase 2 gate (pytest only — extraction-report costs real API spend, run it separately) ---"
	cd backend && $(VENV)/pytest -q tests/test_extract.py

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
