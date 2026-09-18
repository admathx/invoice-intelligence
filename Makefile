.PHONY: up down migrate seed smoke test validate frontend-install frontend-dev

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
	cd synthetic && ../backend/$(VENV)/python generate.py

smoke:
	PYTHONPATH=backend backend/$(VENV)/python backend/scripts/smoke.py

test:
	cd backend && $(VENV)/pytest -q

validate:
	cd backend && $(VENV)/pytest -q tests/test_skeleton.py
	$(MAKE) smoke

frontend-install:
	cd frontend && npm install

frontend-dev:
	cd frontend && npm run dev
