#!/usr/bin/env bash
# F10: CI workflow silencers require rationale.
#
# GitHub Actions YAML supports several mechanisms that hide failures from
# the merge gate. Each is sometimes legitimate (Codecov outage shouldn't
# block the merge) but each is also a tempting agent shortcut. F10
# rejects bare uses; the same-line trailing comment must explain why
# the silencer is load-bearing.
#
# Patterns covered (YAML keys only — pytest-CLI silencers like
# --cov-fail-under=0 are line-continuation arguments where same-line
# comments don't render; their rationale lives in the surrounding
# YAML #-comment block and is reviewed by F3 if it appears in a
# Python file):
#   - continue-on-error: true        (job-level / step-level mute)
#   - fail_ci_if_error: false        (codecov action mute)
#
# Accepted:
#   continue-on-error: true  # codecov outage shouldn't block merge
#   fail_ci_if_error: false  # see #142 - upload races on matrix
#
# Rejected:
#   continue-on-error: true
#   fail_ci_if_error: false
#
# F10 file-level: a workflow file is a violation if ANY silencer line
# in it has no trailing comment. The remediation is either to delete
# the silencer (preferred — make CI fail loudly) or document why.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="Add a same-line rationale comment to every CI workflow silencer:
  continue-on-error: true  # <why this step's failure is acceptable>
  fail_ci_if_error: false  # <why an upload error should not fail CI>
  --cov-fail-under=0  # <why this scope cannot meet the project floor>

Each rationale is the receipt that the silencer is intentional, not a
quiet bypass of the merge gate. Prefer DELETING the silencer entirely
when the underlying failure is real and should block the merge."

# A line is a violation if the silencer pattern matches AND there is no
# trailing #-comment AFTER the value (allowing trailing whitespace
# before the EOL).
#
# We grep -lE for the bare form (no comment after the value) — anything
# with a trailing comment passes.
{
    grep -rEl 'continue-on-error:[[:space:]]*true[[:space:]]*$' .github/workflows/ --include='*.yml' 2>/dev/null
    grep -rEl 'fail_ci_if_error:[[:space:]]*false[[:space:]]*$' .github/workflows/ --include='*.yml' 2>/dev/null
} | sort -u | arch_gate "workflow-silencers-have-rationale" "$REMEDIATION"
