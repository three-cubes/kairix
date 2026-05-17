#!/usr/bin/env bash
# F2: No monkeypatch.setenv("KAIRIX_*") in tests.
#
# Per the boundary-only KairixPaths pattern (#139), env vars are read once at
# the boundary into KairixPaths. Tests construct KairixPaths directly via
# tests.fakes.FakePaths, never via process-env mutation.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="Refactor to constructor-injected FakePaths from tests/fakes.py
(no monkeypatch.setenv / setattr / delenv on KAIRIX_* keys) to pass.

fix: replace ``monkeypatch.setenv('KAIRIX_...', ...)`` with explicit
construction of a ``FakePaths`` from tests/fakes.py and pass it as the
``paths=`` argument to the use case. If the production function reads
the env var directly, refactor it to accept ``paths: KairixPaths`` as
an explicit argument — the boundary-only pattern from #139.
next: re-run ``bash scripts/checks/check-no-env-monkeypatch.sh`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh \"test(<area>): use FakePaths instead of env monkeypatch\"

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read.

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  result = some_use_case()

If the production code requires process-env mutation to be testable,
that is the test-shaped-API smell from #139 — refactor the production
function to accept ``paths: KairixPaths`` as an explicit argument."

# Delegate to AST-based detector — a grep-based check would match
# docstring text and produce false positives.
python3 "${SCRIPT_DIR}/check_no_env_monkeypatch.py" \
    | arch_gate "no-env-monkeypatch" "$REMEDIATION"
