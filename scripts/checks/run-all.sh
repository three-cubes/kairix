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

# F4
bash "${SCRIPT_DIR}/check-env-reads-stay-in-paths.sh" || overall=1

# F3
bash "${SCRIPT_DIR}/check-suppressions-have-rationale.sh" || overall=1

# F5 — AST-based
python3 "${SCRIPT_DIR}/check_no_internal_imports.py" || overall=1

# F6 — AST-based
python3 "${SCRIPT_DIR}/check_no_test_only_kwargs.py" || overall=1

# F8 — AST-based
python3 "${SCRIPT_DIR}/check_test_markers.py" || overall=1

# F10 — workflow YAML silencer rationale (shell + grep)
bash "${SCRIPT_DIR}/check-workflow-silencers-have-rationale.sh" || overall=1

# F11 — test skip rationale (AST)
python3 "${SCRIPT_DIR}/check_test_skip_rationale.py" || overall=1

# F12 — BDD happy-path coverage
python3 "${SCRIPT_DIR}/check_bdd_happy_path.py" || overall=1

# F13 — BDD no implementation symbols
python3 "${SCRIPT_DIR}/check_bdd_no_implementation_leaks.py" || overall=1

# F14 — sonar.issue.ignore entries require rationale
python3 "${SCRIPT_DIR}/check_sonar_ignore_rationale.py" || overall=1

# F15 — no logging of secret-named variables in plaintext
python3 "${SCRIPT_DIR}/check_no_logging_secrets.py" || overall=1

# F16 — cognitive complexity per function
python3 "${SCRIPT_DIR}/check_cognitive_complexity.py" || overall=1

# F17 — no duplicated string literal ≥10 chars / ≥3 occurrences
python3 "${SCRIPT_DIR}/check_no_duplicate_string.py" || overall=1

# F18 — no commented-out code
python3 "${SCRIPT_DIR}/check_no_commented_out_code.py" || overall=1

# F19 — unused parameter must be _ prefixed
python3 "${SCRIPT_DIR}/check_unused_params_named.py" || overall=1

# F20 — empty function body requires docstring or intent comment
python3 "${SCRIPT_DIR}/check_empty_body_intent.py" || overall=1

# F21 — actionable-feedback marker rule for check scripts
python3 "${SCRIPT_DIR}/check_actionable_feedback.py" || overall=1

# F22 — repo path naming conventions per tree
python3 "${SCRIPT_DIR}/check_path_naming.py" || overall=1

# F23 — every top-level directory has a README.md
python3 "${SCRIPT_DIR}/check_readme_coverage.py" || overall=1

# F24 — no imports of tests.* in kairix production code
python3 "${SCRIPT_DIR}/check_no_test_imports_in_prod.py" || overall=1

# G9 — every services/<name>/ has a README.md (Go side; mirrors F23)
python3 "${SCRIPT_DIR}/check_go_readme_coverage.py" || overall=1

# G1 — every Go binary exposes --version
python3 "${SCRIPT_DIR}/check_go_version_flag.py" || overall=1

# F7 — needs coverage.xml. Skip if not present or skip flag set.
if [[ "$skip_coverage" -eq 0 ]]; then
    if [[ -f "${REPO_ROOT}/coverage.xml" ]]; then
        python3 "${SCRIPT_DIR}/check_per_file_coverage.py" "${REPO_ROOT}/coverage.xml" || overall=1
    else
        printf '\033[0;33mskip [arch:per-file-coverage-floor]\033[0m — coverage.xml not found.\n'
        printf '   Run: pytest --cov=kairix --cov-report=xml first, then re-run this check.\n'
    fi
fi

echo
if [[ "$overall" -eq 0 ]]; then
    printf '\033[0;32m=== All architecture fitness functions passed ===\033[0m\n'
else
    printf '\033[0;31m=== Architecture fitness functions FAILED ===\033[0m\n'
fi
exit "$overall"
