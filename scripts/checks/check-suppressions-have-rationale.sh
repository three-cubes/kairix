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

REMEDIATION="Add an inline rationale after the suppression. Format:
  x = 1  # NOSONAR — <why this rule does not apply>
  x = 1  # noqa: BLE001  # <why this rule does not apply>
  x = 1  # pragma: no cover  # <why this line is genuinely untestable>
  x = 1  # type: ignore[union-attr]  # <why mypy is wrong here>
  x = 1  # nosec B607  # <why bandit's concern does not apply>
The rationale is read at every code review and is the receipt that the
suppression is deliberate, not a way to silence a real warning."

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
