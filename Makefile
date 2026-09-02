# Quant Intelligence Platform — developer entry points.
.DEFAULT_GOAL := help
PY ?= .venv/bin/python
COMPOSE ?= docker compose

# Local defaults so `make run` / `make migrate` work with no infrastructure.
# Override on the command line: make migrate QIP_DATABASE_URL=postgresql+asyncpg://...
QIP_DATABASE_URL ?= sqlite+aiosqlite:///./var/qip.db
QIP_JOB_EXECUTION_MODE ?= eager
QIP_LOG_FORMAT ?= console
export QIP_DATABASE_URL QIP_JOB_EXECUTION_MODE QIP_LOG_FORMAT

.PHONY: help
help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ local dev
.PHONY: venv
venv:  ## Create the virtualenv and install everything
	python3 -m venv --without-pip .venv
	python3 -m pip --python .venv/bin/python install -q pip setuptools wheel
	$(PY) -m pip install -e ".[dev,validation]"

.PHONY: run
run:  ## Run the API against a local SQLite database with eager jobs
	@mkdir -p var
	$(PY) -m uvicorn apps.api.main:app --reload

.PHONY: migrate
migrate:  ## Apply migrations to the configured database
	@mkdir -p var
	$(PY) -m alembic upgrade head

.PHONY: revision
revision:  ## Autogenerate a migration: make revision m="add surfaces"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------- tests
.PHONY: test
test:  ## Run the default suite (no benchmarks)
	$(PY) -m pytest -q -m "not performance"

.PHONY: test-quant
test-quant:  ## Numerical correctness only
	$(PY) -m pytest -q tests/quant_validation

.PHONY: test-regression
test-regression:  ## Golden-file regression
	$(PY) -m pytest -q -m regression

.PHONY: bench
bench:  ## Benchmarks (printed, not asserted)
	$(PY) -m pytest -q -m performance -s

.PHONY: golden-diff
golden-diff:  ## Report drift against the committed golden files
	$(PY) scripts/regen_golden.py --diff

.PHONY: golden-accept
golden-accept:  ## Rewrite the golden files (review the diff!)
	$(PY) scripts/regen_golden.py --accept all

.PHONY: fixtures
fixtures:  ## Regenerate the committed test fixtures
	$(PY) scripts/generate_test_data.py

# --------------------------------------------------------------------- checks
.PHONY: lint
lint:  ## Lint and format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

.PHONY: fix
fix:  ## Apply lint fixes and formatting
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

.PHONY: layering
layering:  ## Enforce the architecture layering rules
	$(PY) scripts/check_layering.py

.PHONY: check
check: lint layering test  ## Everything CI runs

# ------------------------------------------------------------------- frontend
.PHONY: web-install
web-install:  ## Install frontend dependencies
	cd web && npm install

.PHONY: web-dev
web-dev:  ## Run the frontend dev server
	cd web && npm run dev

.PHONY: web-build
web-build:  ## Build the frontend
	cd web && npm run build

# ---------------------------------------------------------------------- docker
.PHONY: up
up:  ## Start the whole stack
	$(COMPOSE) up -d --build

.PHONY: down
down:  ## Stop the stack
	$(COMPOSE) down

.PHONY: logs
logs:  ## Tail API and worker logs
	$(COMPOSE) logs -f api worker

.PHONY: clean
clean:  ## Remove build and cache artefacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache var/*.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
