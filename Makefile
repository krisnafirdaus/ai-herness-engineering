# Convenience targets. The harness core needs only the Python stdlib; `setup`
# installs the OPTIONAL extras (API server, real LLM SDKs, Docker, Langfuse).
.DEFAULT_GOAL := help
PY ?= python3
REPO ?= ./dummy-repos/python-api-sample
TASK ?= Add request validation to the user creation endpoint

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install optional extras (FastAPI, SDKs, docker, langfuse, pytest)
	$(PY) -m pip install -r requirements.txt pytest

.PHONY: demo
demo: ## Run the headline demo (offline mock) + show traces, log, diff
	@$(PY) -m src.main run --repo $(REPO) --task "$(TASK)"
	@echo "\n──────── TELEMETRY ────────"
	@$(PY) -m src.main traces --run-id $$($(PY) -m src.main list | head -1 | awk '{print $$1}')
	@echo "\n──────── EVENT LOG ────────"
	@$(PY) -m src.main log    --run-id $$($(PY) -m src.main list | head -1 | awk '{print $$1}')
	@echo "\n──────── DIFF ─────────────"
	@$(PY) -m src.main diff   --run-id $$($(PY) -m src.main list | head -1 | awk '{print $$1}')

.PHONY: demo-node
demo-node: ## Run the demo against the Node.js dummy repo
	$(PY) -m src.main run --repo ./dummy-repos/node-api-sample \
		--task "Add validation to the user creation endpoint"

.PHONY: run
run: ## Run a custom task:  make run REPO=<url|path> TASK="..."
	$(PY) -m src.main run --repo $(REPO) --task "$(TASK)"

.PHONY: api
api: ## Start the API control plane on :8000
	uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --reload

.PHONY: worker
worker: ## Start a worker (recover stuck runs, then poll the queue)
	$(PY) -m src.worker

.PHONY: recover
recover: ## Crash recovery: resume ALL non-terminal runs and exit
	$(PY) -m src.main recover

.PHONY: test
test: ## Run the test suite (auto-bootstraps pytest into .venv if missing)
	@if $(PY) -c "import pytest" >/dev/null 2>&1; then \
		$(PY) -m pytest -q; \
	else \
		echo "→ pytest not found; bootstrapping an isolated .venv (pytest only)"; \
		$(PY) -m venv .venv && .venv/bin/python -m pip -q install pytest && .venv/bin/python -m pytest -q; \
	fi

.PHONY: sandbox-image
sandbox-image: ## Build the Docker sandbox image (enables the docker sandbox)
	docker build -t harness-sandbox:latest -f infra/Dockerfile.sandbox .

.PHONY: compose-up
compose-up: ## Bring up the production-shaped topology (api+worker+pg+redis)
	cd infra && docker compose up -d --build

.PHONY: compose-down
compose-down: ## Tear down the compose topology
	cd infra && docker compose down -v

.PHONY: clean
clean: ## Remove the local state DB and run workspaces
	rm -rf workspaces harness.sqlite3 harness.sqlite3-wal harness.sqlite3-shm
