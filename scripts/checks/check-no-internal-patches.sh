#!/usr/bin/env bash
# F1: No @patch on kairix internal code.
#
# Tests must not patch kairix.* — refactor to use constructor injection or a
# Protocol seam from kairix.core.protocols. Stdlib boundaries (os.*, builtins.*)
# and external SDK boundaries (openai.*, httpx.*) remain allowed.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="Refactor: tests should construct the unit under test with explicit
fakes from tests/fakes.py, not patch kairix internals. If the
production class lacks a constructor seam, add one (same shape as
GoldBuilder(llm_judge=, retriever=))."

grep -rEl '(@patch|with patch)\("kairix\.' tests/ --include='*.py' 2>/dev/null \
    | arch_gate "no-internal-patches" "$REMEDIATION"
