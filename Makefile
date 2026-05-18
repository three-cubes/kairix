.PHONY: lint format check test test-unit test-bdd test-contract test-integration type-check security commit clean \
        go-modules go-fmt go-vet go-lint go-test go-build go-check

# Combined quality check — run all linting, formatting, and type checks
lint: lint-check format-check type-check

# Individual checks
lint-check:
	ruff check kairix/ tests/

format-check:
	ruff format --check kairix/ tests/

type-check:
	mypy kairix/ --strict

# Auto-fix formatting and imports
format:
	ruff check kairix/ tests/ --fix
	ruff format kairix/ tests/

# Tests by category
test: test-unit test-bdd test-contract

test-unit:
	pytest tests/ -m unit -x --timeout=30

test-bdd:
	pytest tests/ -m bdd -x --timeout=30

test-contract:
	pytest tests/ -m contract -x --timeout=30

test-integration:
	pytest tests/ -m integration -x --timeout=60

test-all:
	pytest tests/ -m "unit or bdd or contract" -x --timeout=30

# Security
security:
	detect-secrets scan --baseline .secrets.baseline
	python3 -m bandit -r kairix/ -ll --quiet
	bash scripts/pre-commit-confidential-check.sh

# Full pre-commit gate
check: lint test-all security

# Gated commit — use: make commit MSG="your message"
commit:
	bash scripts/safe-commit.sh "$(MSG)"

# Clean build artifacts
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache htmlcov coverage.xml
	find services -type d -name dist -exec rm -rf {} + 2>/dev/null || true

# ── Go gates (per docs/architecture/go-integration-plan.md) ───────────────
# Each target discovers every services/<name>/go.mod and runs the gate
# per-module. Empty services/ → no-op (exit 0). Matches the CI workflow
# behaviour so local and CI are aligned.

go-modules:
	@find services -mindepth 2 -maxdepth 2 -name go.mod -exec dirname {} \; 2>/dev/null || true

go-fmt:
	@for m in $$($(MAKE) -s go-modules); do \
	  echo "→ gofmt -s -l $$m"; \
	  diff=$$(cd $$m && gofmt -s -l .); \
	  if [ -n "$$diff" ]; then echo "$$diff"; echo "fix: gofmt -s -w $$m"; exit 1; fi; \
	done

go-vet:
	@for m in $$($(MAKE) -s go-modules); do \
	  echo "→ go vet $$m"; \
	  (cd $$m && go vet ./...) || exit 1; \
	done

go-lint:
	@mods=$$($(MAKE) -s go-modules); \
	if [ -z "$$mods" ]; then echo "(no Go modules — go-lint no-op)"; exit 0; fi; \
	command -v golangci-lint >/dev/null || { echo "golangci-lint not installed — see https://golangci-lint.run/install"; exit 1; }; \
	for m in $$mods; do \
	  echo "→ golangci-lint run $$m"; \
	  (cd $$m && golangci-lint run --config=$(CURDIR)/.golangci.yml --timeout=5m) || exit 1; \
	done

go-test:
	@for m in $$($(MAKE) -s go-modules); do \
	  echo "→ go test -race -cover $$m"; \
	  (cd $$m && go test -race -covermode=atomic -coverprofile=coverage.out ./... && go tool cover -func=coverage.out | tail -1) || exit 1; \
	done

go-build:
	@for m in $$($(MAKE) -s go-modules); do \
	  echo "→ go build $$m"; \
	  (cd $$m && mkdir -p dist && for cmd in cmd/*/; do \
	    name=$$(basename $$cmd); \
	    go build -trimpath -ldflags "-s -w -X main.version=dev" -o "dist/$$name" "./$$cmd" || exit 1; \
	    echo "    built dist/$$name"; \
	  done) || exit 1; \
	done

# Aggregate — local equivalent of the CI go-quality.yml gate
go-check: go-fmt go-vet go-lint go-test go-build
