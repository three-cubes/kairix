#!/usr/bin/env bash
# Architecture fitness function harness — run all checks; aggregate exit code.
#
# Each check fails on net-new violations vs its baseline; pre-existing
# violations are grandfathered. The aggregate exit code is non-zero if any
# individual check fails.
#
# Usage:
#   bash scripts/checks/run-all.sh                # run all
#   bash scripts/checks/run-all.sh --skip-coverage  # skip F7 (needs coverage.xml)

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

skip_coverage=0
for arg in "$@"; do
    case "$arg" in
        --skip-coverage) skip_coverage=1 ;;
    esac
done

echo "=== Architecture fitness functions ==="
overall=0

# F1
bash "${SCRIPT_DIR}/check-no-internal-patches.sh" || overall=1

# F2
bash "${SCRIPT_DIR}/check-no-env-monkeypatch.sh" || overall=1

# F3
bash "${SCRIPT_DIR}/check-suppressions-have-rationale.sh" || overall=1

# F5 — AST-based
python3 "${SCRIPT_DIR}/check_no_internal_imports.py" || overall=1

# F6 — AST-based
python3 "${SCRIPT_DIR}/check_no_test_only_kwargs.py" || overall=1

# F7 — needs coverage.xml. Skip if not present or skip flag set.
if [ "$skip_coverage" -eq 0 ]; then
    if [ -f "${REPO_ROOT}/coverage.xml" ]; then
        python3 "${SCRIPT_DIR}/check_per_file_coverage.py" "${REPO_ROOT}/coverage.xml" || overall=1
    else
        printf '\033[0;33mskip [arch:per-file-coverage-floor]\033[0m — coverage.xml not found.\n'
        printf '   Run: pytest --cov=kairix --cov-report=xml first, then re-run this check.\n'
    fi
fi

echo
if [ "$overall" -eq 0 ]; then
    printf '\033[0;32m=== All architecture fitness functions passed ===\033[0m\n'
else
    printf '\033[0;31m=== Architecture fitness functions FAILED ===\033[0m\n'
fi
exit "$overall"
