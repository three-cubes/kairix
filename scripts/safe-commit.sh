#!/usr/bin/env bash
# safe-commit.sh — commit only if ALL quality gates pass.
#
# Usage:
#   bash scripts/safe-commit.sh "commit message"
#
# Gates (in order, fail-fast):
#   1. ruff lint (includes isort import ordering via I rules)
#   2. ruff format (black-compatible formatting)
#   3. mypy --strict type checking
#   4. pytest (unit + bdd + contract)
#   5. architecture fitness functions (F1-F6; F7 needs coverage.xml)
#   6. detect-secrets
#   7. confidential data check

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: bash scripts/safe-commit.sh \"commit message\""
    exit 1
fi

MESSAGE="$1"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# 0. Empty-stage guard. safe-commit.sh does not auto-stage; running it
# without `git add` produced silent no-op "commits" that masked real
# failures (#208 side-finding). Fail loud here instead.
if git diff --cached --quiet; then
    echo -e "${RED}FAIL${NC}: nothing staged for commit"
    echo "fix: stage files with 'git add <paths>' before running safe-commit.sh"
    echo "next: 'git status' to see what's modified but not yet staged"
    exit 1
fi

echo "=== Quality gates ==="

# 1. Lint (includes isort via ruff I rules)
# Scope kairix/ + tests/ + scripts/ to match what pre-commit's ruff hook
# scans in CI — local-vs-CI divergence here has cost round-trips already.
echo -n "  ruff lint... "
ruff check kairix/ tests/ scripts/ --quiet 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff check kairix/ tests/ scripts/ --fix"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 2. Format (black-compatible via ruff format)
echo -n "  ruff format... "
ruff format --check kairix/ tests/ scripts/ >/dev/null 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff format kairix/ tests/ scripts/"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 2b. gofmt on every Go service (when present). Auto-discovered: any
# services/<name>/go.mod triggers a gofmt check on that module. Mirrors
# what the remote 'Go quality' workflow does — keeping this local saves
# a CI round-trip when a Go change is in the staged diff.
if command -v gofmt >/dev/null 2>&1; then
    while IFS= read -r gomod; do
        svc_dir="$(dirname "$gomod")"
        echo -n "  gofmt -s ($svc_dir)... "
        unformatted=$(gofmt -s -l "$svc_dir" 2>&1)
        if [[ -n "$unformatted" ]]; then
            echo -e "${RED}FAIL${NC}"
            echo "$unformatted" | sed 's/^/  /'
            echo "Run: gofmt -s -w $svc_dir"
            exit 1
        fi
        echo -e "${GREEN}OK${NC}"
    done < <(find services -mindepth 2 -maxdepth 2 -name go.mod 2>/dev/null)
fi

# 3. Type checking (strict — matches CI)
echo -n "  mypy strict... "
MYPY_OUT=$(mypy kairix/ --strict 2>&1)
if echo "$MYPY_OUT" | grep -q "error"; then
    echo -e "${RED}FAIL${NC}"
    echo "$MYPY_OUT" | grep "error" | head -10
    echo "Run: mypy kairix/ --strict"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# 4. Tests
echo -n "  tests... "
TEST_OUT=$(python3 -m pytest tests/ -x --timeout=30 -m "unit or bdd or contract" 2>&1)
if echo "$TEST_OUT" | grep -qE "[0-9]+ failed"; then
    echo -e "${RED}FAIL${NC}"
    echo "$TEST_OUT" | grep -E "FAILED|passed|failed" | tail -10
    exit 1
fi
if ! echo "$TEST_OUT" | grep -qE "[0-9]+ passed"; then
    echo -e "${RED}FAIL${NC} (no tests collected)"
    exit 1
fi
PASSED=$(echo "$TEST_OUT" | grep -oE '[0-9]+ passed')
echo -e "${GREEN}OK${NC} ($PASSED)"

# 5. Architecture fitness functions (F1-F6 — F7 runs in CI on the coverage.xml
# emitted by the unit-and-type job).
echo -n "  arch fitness... "
ARCH_OUT=$(bash scripts/checks/run-all.sh --skip-coverage 2>&1) || {
    echo -e "${RED}FAIL${NC}"
    echo "$ARCH_OUT" | tail -30
    echo "See docs/architecture/fitness-functions.md for remediation."
    exit 1
}
echo -e "${GREEN}OK${NC}"

# 6. Secret detection — pre-commit hook mirrors CI; do not invoke `detect-secrets scan`
# directly here (it overwrites the baseline and only scans the path you pass it).
echo -n "  secrets... "
SECRETS_OUT=$(pre-commit run detect-secrets --all-files 2>&1) || true
if echo "$SECRETS_OUT" | grep -q "Failed"; then
    echo -e "${RED}FAIL${NC}"
    echo "$SECRETS_OUT" | tail -20
    echo "If a test fixture is a false positive, mark with: # pragma: allowlist secret"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# 7. Confidential check
echo -n "  confidential... "
bash scripts/pre-commit-confidential-check.sh 2>/dev/null || { echo -e "${RED}FAIL${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

echo ""
echo -e "${GREEN}All gates passed. Committing.${NC}"
git commit -m "$MESSAGE"
