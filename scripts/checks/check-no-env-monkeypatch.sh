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

REMEDIATION="Refactor: pass paths as a constructor argument or use FakePaths
from tests/fakes.py. The production code must not require process-env
mutation to be testable — that's the test-shaped-API smell #139 reverted."

# Delegate to AST-based detector (resolves #217 — grep matched docstring text).
python3 "${SCRIPT_DIR}/check_no_env_monkeypatch.py" \
    | arch_gate "no-env-monkeypatch" "$REMEDIATION"
