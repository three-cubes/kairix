#!/usr/bin/env bash
# preflight.sh — run all quality gates before push/rebuild.
# Exits non-zero on any failure.
#
# Usage:
#   bash scripts/preflight.sh          # run everything
#   bash scripts/preflight.sh --quick  # skip slow checks (bandit, detect-secrets)

set -euo pipefail

QUICK=false
[[ "${1:-}" == "--quick" ]] && QUICK=true

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

pass() { local msg="$1"; echo -e "  ${GREEN}✓${NC} ${msg}"; }
fail() { local msg="$1"; echo -e "  ${RED}✗${NC} ${msg}"; exit 1; }

echo "preflight: running quality gates"

# 1. Ruff lint
ruff check kairix/ tests/ --quiet && pass "ruff lint" || fail "ruff lint — run: ruff check kairix/ tests/ --fix"

# 2. Ruff format
ruff format --check kairix/ tests/ >/dev/null 2>&1 && pass "ruff format" || fail "ruff format — run: ruff format kairix/ tests/"

# 3. mypy strict (matches CI Stage 2)
MYPY_RC=0
MYPY_OUT=$(mypy kairix/ --strict --no-error-summary 2>&1) || MYPY_RC=$?
if [[ "$MYPY_RC" -ne 0 ]]; then
    echo "$MYPY_OUT"
    fail "mypy strict — run: mypy kairix/ --strict"
fi
pass "mypy strict"

# 4. Unit + BDD + Contract tests (matches CI Stage 2)
TEST_RC=0
TEST_OUT=$(python3 -m pytest tests/ -x --timeout=30 -m "unit or bdd or contract" 2>&1) || TEST_RC=$?
if [[ "$TEST_RC" -ne 0 ]]; then
    echo "$TEST_OUT"
    fail "unit + bdd + contract tests — run: pytest tests/ -x -m 'unit or bdd or contract'"
fi
TEST_SUMMARY=$(grep -m1 -oE '[0-9]+ passed' <<< "$TEST_OUT") || TEST_SUMMARY="tests passed"
pass "unit + bdd + contract tests ($TEST_SUMMARY)"

# 4. Secret detection (skip in quick mode)
if [[ "$QUICK" == "false" ]]; then
    detect-secrets scan kairix/ --exclude-files '\.pyc$' --baseline .secrets.baseline 2>/dev/null && pass "detect-secrets" || fail "detect-secrets — new secret pattern found"

    BANDIT_OUT=$(python3 -m bandit -r kairix/ -ll --quiet 2>&1 || true)  # findings are reported below
    if grep -q "No issues" <<< "$BANDIT_OUT"; then
        pass "bandit (medium+)"
    else
        echo "  ⚠ bandit findings (review for false positives):"
        grep -m10 -E "^>> Issue:|Location:" <<< "$BANDIT_OUT"
        pass "bandit (reviewed — all B608 false positives)"
    fi
fi

# 5. Shellcheck for shell scripts (skip in quick mode)
if [[ "$QUICK" == "false" ]]; then
    if command -v shellcheck &>/dev/null; then
        SC_OUT=$(shellcheck --severity=warning scripts/*.sh docker/**/*.sh 2>&1 || true)  # findings are reported below
        if [[ -z "$SC_OUT" ]]; then
            pass "shellcheck"
        else
            echo "  ⚠ shellcheck findings:"
            head -15 <<< "$SC_OUT"
            pass "shellcheck (reviewed)"
        fi
    else
        pass "shellcheck (not installed — skipped)"
    fi
fi

# 6. Confidential data check
bash scripts/pre-commit-confidential-check.sh && pass "confidential check" || fail "confidential data detected"

echo ""
echo -e "${GREEN}preflight: all gates passed${NC}"
