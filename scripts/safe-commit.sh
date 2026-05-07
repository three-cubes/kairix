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
#   5. detect-secrets
#   6. confidential data check

set -euo pipefail

if [ $# -lt 1 ]; then
    echo "Usage: bash scripts/safe-commit.sh \"commit message\""
    exit 1
fi

MESSAGE="$1"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo "=== Quality gates ==="

# 1. Lint (includes isort via ruff I rules)
echo -n "  ruff lint... "
ruff check kairix/ tests/ --quiet 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff check kairix/ tests/ --fix"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 2. Format (black-compatible via ruff format)
echo -n "  ruff format... "
ruff format --check kairix/ tests/ >/dev/null 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff format kairix/ tests/"; exit 1; }
echo -e "${GREEN}OK${NC}"

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

# 5. Secret detection — pre-commit hook mirrors CI; do not invoke `detect-secrets scan`
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

# 6. Confidential check
echo -n "  confidential... "
bash scripts/pre-commit-confidential-check.sh 2>/dev/null || { echo -e "${RED}FAIL${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

echo ""
echo -e "${GREEN}All gates passed. Committing.${NC}"
git commit -m "$MESSAGE"
