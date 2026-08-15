UV := uv run
PYTHON_VERSIONS := 3.11 3.12 3.13 3.14

.DEFAULT_GOAL := help
.PHONY: help sync format lint test test-all changelog-test check build clean

help: ## List the targets
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sort | awk -F':.*##' '{printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

sync: ## Install the venv from uv.lock
	uv sync --locked

format: ## Rewrite files to the ruff format and apply safe lint fixes
	$(UV) ruff format .
	$(UV) ruff check --fix .

lint: ## Run the checks the CI lint job runs
	$(UV) ruff format --check .
	$(UV) ruff check .
	$(UV) ty check

test: ## Run the tests on the default Python
	$(UV) pytest -q

test-all: ## Run the tests on every Python in the CI matrix
	@for v in $(PYTHON_VERSIONS); do \
		echo "== python $$v"; \
		uv run --locked --python $$v --isolated pytest -q || exit 1; \
	done

changelog-test: ## Run the tests for the changelog scripts in scripts/
	./scripts/changelog-for-release.test.sh

check: lint test changelog-test ## Everything the CI lint and test jobs run, on one Python

build: ## Build the sdist and wheel into dist/
	uv build

clean: ## Delete build and cache directories
	rm -rf dist build .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
