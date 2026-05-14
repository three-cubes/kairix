#!/usr/bin/env bash
# F3: Suppressions require rationale.
#
# A bare suppression comment (no rationale) is rejected. Covers every
# common silencer pattern available to agents:
#   - ``# NOSONAR``                   (Sonar)
#   - ``# noqa`` / ``# noqa: CODE``   (ruff / flake8)
#   - ``# pragma: no cover``          (coverage.py — F7 bypass)
#   - ``# type: ignore`` / ``# type: ignore[code]``   (mypy)
#   - ``# nosec`` / ``# nosec: SXXX`` (bandit — security bypass)
#
# Accepted: ``x = 1  # NOSONAR — internal log path; not user-controlled``
# Rejected: ``x = 1  # NOSONAR``
#
# The accompanying same-line rationale documents WHY the rule doesn't
# apply, so future readers can tell whether the suppression is still
# load-bearing. F3 is the catch-all rationale gate; specific silencer
# scopes (mypy strict, bandit, coverage.py) live in their own configs
# but every per-line suppression flows through here.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="Refactor to add an inline rationale after each suppression
(or delete the suppression entirely if the underlying warning is real) to pass.

fix: add an inline rationale after each bare suppression (everything
after the suppression token counts; an em-dash + one-line justification
is the canonical shape) — or delete the suppression entirely if the
underlying warning is a real issue you should address.
next: re-run ``bash scripts/checks/check-suppressions-have-rationale.sh``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh \"chore(<area>): document suppression rationale\"

Pass example:
  x = 1  # NOSONAR — internal log path; not user-controlled
  result = requests.get(url)  # noqa: BLE001 — caller pins context

Forbidden example:
  x = 1  # NOSONAR
  result = requests.get(url)  # noqa: BLE001

Each rationale is the receipt that the suppression is deliberate, not a
way to silence a real warning. The rationale is reviewed every time the
line is touched. Patterns covered:
  # NOSONAR                          (Sonar)
  # noqa  or  # noqa: CODE           (ruff / flake8)
  # pragma: no cover                 (coverage.py — F7 bypass)
  # type: ignore  or  # type: ignore[code]   (mypy)
  # nosec  or  # nosec B607          (bandit)"

# Match a bare suppression at end-of-line (allowing trailing whitespace).
# Reject only the bare form; rationale (any non-whitespace after the
# suppression token) passes.
{
    grep -rEl '#[[:space:]]*NOSONAR[[:space:]]*$' kairix/ tests/ scripts/ --include='*.py' 2>/dev/null
    grep -rEl '#[[:space:]]*noqa(:[A-Z0-9,]+)?[[:space:]]*$' kairix/ tests/ scripts/ --include='*.py' 2>/dev/null
    grep -rEl '#[[:space:]]*pragma:[[:space:]]*no cover[[:space:]]*$' kairix/ tests/ scripts/ --include='*.py' 2>/dev/null
    grep -rEl '#[[:space:]]*type:[[:space:]]*ignore(\[[A-Za-z0-9,_-]+\])?[[:space:]]*$' kairix/ tests/ scripts/ --include='*.py' 2>/dev/null
    grep -rEl '#[[:space:]]*nosec([[:space:]]+B[0-9]+|:[[:space:]]*B?[0-9]+)?[[:space:]]*$' kairix/ tests/ scripts/ --include='*.py' 2>/dev/null
} | sort -u | arch_gate "suppressions-have-rationale" "$REMEDIATION"
