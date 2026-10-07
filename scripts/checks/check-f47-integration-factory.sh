#!/usr/bin/env bash
# F47: integration tests build pipelines through kairix.core.factory.build_*.
#
# Direct construction of SearchPipeline / EmbedPipeline / ConnectorPipeline /
# IngestPipeline in tests/integration/ is allowed only in *_contract.py
# (single-layer boundary proofs) or under tests/contracts/.
#
# This wrapper delegates to the AST detector; the Python script owns the
# gate verdict and prints the F21 action-marked remediation on failure.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="F47: tests/integration/<file>.py constructs <Pipeline> directly.
fix: use kairix.core.factory.build_<pipeline>(paths=FakePaths(...)).
next: see tests/integration/test_vec_index_lifecycle.py for the canonical
pattern, and docs/architecture/test-discipline-hardening.md §4.2.
run: bash scripts/checks/check-f47-integration-factory.sh"

# The detector embeds its own gate (gate() from the tc_fitness package; any violation fails)
# — mirrors the F30 pattern. Echo the remediation if the script exits
# non-zero so operators get the action markers regardless of how the
# wrapper is invoked.
if ! python3 "${SCRIPT_DIR}/check_f47_integration_factory.py"; then
    printf '\n%s\n' "$REMEDIATION"
    exit 1
fi
