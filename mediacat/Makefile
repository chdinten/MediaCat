# MediaCat Makefile.
# Run `make help` for the list of targets.
.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
.SHELLFLAGS := -Eeuo pipefail -c

PY      ?= python3.12
VENV    ?= .venv
PIP     := $(VENV)/bin/pip
PYTHON  := $(VENV)/bin/python
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
BANDIT  := $(VENV)/bin/bandit
AUDIT   := $(VENV)/bin/pip-audit
PYTEST  := $(VENV)/bin/pytest
PDOC    := $(VENV)/bin/pdoc
MKDOCS  := $(VENV)/bin/mkdocs

.PHONY: help bootstrap setup venv deps-sync data-init \
        lint format typecheck security test test-fast \
        docs docs-build docs-serve docs-api \
        up down restart logs ps config-check db-migrate db-status \
        ollama-up ollama-gpu-up ollama-down ollama-models \
        clean distclean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} \
	      /^[a-zA-Z0-9_.-]+:.*##/ { printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

## ---- System bootstrap ---------------------------------------------------

bootstrap: ## Run Ubuntu system bootstrap (requires sudo)
	./scripts/ubuntu-bootstrap.sh

data-init: ## Create / verify persistent data tree on host
	./scripts/data-init.sh

## ---- Python environment -------------------------------------------------

$(VENV)/bin/activate:
	$(PY) -m venv $(VENV)
	$(PIP) install --upgrade pip wheel setuptools

venv: $(VENV)/bin/activate ## Create local virtualenv

setup: venv ## Install project + dev deps + pre-commit hooks
	$(PIP) install -e ".[dev,docs]"
	$(VENV)/bin/pre-commit install --install-hooks
	@echo "Setup complete. Activate with: source $(VENV)/bin/activate"

deps-sync: venv ## Re-sync dependencies from pyproject.toml
	$(PIP) install -e ".[dev,docs]"

## ---- Code quality -------------------------------------------------------

lint: ## Ruff lint
	$(RUFF) check src tests

format: ## Ruff format + import sort
	$(RUFF) format src tests
	$(RUFF) check --fix --select I src tests

typecheck: ## Mypy strict
	$(MYPY) src

security: ## Bandit + pip-audit
	$(BANDIT) -q -r src -c pyproject.toml
	$(AUDIT) --strict --disable-pip || true

test: ## Pytest with coverage
	$(PYTEST)

test-fast: ## Pytest excluding slow/integration
	$(PYTEST) -m "not slow and not integration"

## ---- Documentation ------------------------------------------------------

docs-api: venv ## Generate API reference with pdoc -> docs/reference/
	$(PDOC) -o docs/reference -d google --no-show-source src/mediacat

docs-build: docs-api ## Build MkDocs site to site/
	$(MKDOCS) build --strict -f docs/mkdocs.yml

docs-serve: docs-api ## Live-preview docs on http://127.0.0.1:8800
	$(MKDOCS) serve -a 127.0.0.1:8800 -f docs/mkdocs.yml

docs: docs-build ## Alias for docs-build

## ---- Database migrations ------------------------------------------------

db-migrate: ## Apply all pending Alembic migrations
	docker compose --env-file .env -f deploy/docker-compose.yaml \
	    exec -e PGHOST=postgres -e PGUSER=mediacat_migrator -e PGDATABASE=mediacat \
	    app python -m alembic -c alembic.ini upgrade head

db-status: ## Show current Alembic migration status
	docker compose --env-file .env -f deploy/docker-compose.yaml \
	    exec -e PGHOST=postgres -e PGUSER=mediacat_migrator -e PGDATABASE=mediacat \
	    app python -m alembic -c alembic.ini current

## ---- Docker Compose (Section 2+) ----------------------------------------

up: ## Bring the dev stack up
	./scripts/dev-up.sh

down: ## Tear the dev stack down (data preserved)
	./scripts/dev-down.sh

restart: down up ## Restart the dev stack

logs: ## Tail logs
	docker compose --env-file .env -f deploy/docker-compose.yaml logs -f --tail=200

ps: ## Compose ps
	docker compose --env-file .env -f deploy/docker-compose.yaml ps

config-check: ## Validate compose config
	docker compose --env-file .env -f deploy/docker-compose.yaml config -q
	@echo "compose config OK"

## ---- Ollama (local LLM / VLM; optional profile) -------------------------

ollama-up: ## Start core stack + Ollama + Open WebUI (CPU)
	MEDIACAT_OLLAMA=1 ./scripts/dev-up.sh

ollama-gpu-up: ## Start core stack + Ollama + Open WebUI (NVIDIA GPU)
	MEDIACAT_OLLAMA=1 MEDIACAT_OLLAMA_GPU=1 ./scripts/dev-up.sh

ollama-down: ## Tear down including Ollama services
	docker compose --env-file .env -f deploy/docker-compose.yaml --profile ollama down

ollama-models: ## (Re-)pull VLM + OCR models into the running Ollama server
	docker compose --env-file .env -f deploy/docker-compose.yaml --profile ollama \
	    run --rm ollama-pull

## ---- Cleanup ------------------------------------------------------------

clean: ## Remove build artefacts and caches
	rm -rf build dist site docs/reference .mypy_cache .ruff_cache .pytest_cache \
	       coverage.xml .coverage htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

distclean: clean ## Also remove venv
	rm -rf $(VENV)
