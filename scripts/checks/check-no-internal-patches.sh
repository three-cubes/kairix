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

REMEDIATION="Refactor to constructor injection with a fake from tests/fakes.py
(no @patch / with patch on kairix.* targets) to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. ``SearchPipeline(retriever=FakeRetriever(...))``)
instead of patching the internal symbol. If the production class
lacks a constructor seam, add one — same shape as
GoldBuilder(llm_judge=, retriever=, db_path=).
next: re-run ``bash scripts/checks/check-no-internal-patches.sh`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh \"test(<area>): inject fake instead of patching internals\"

If the production class lacks a constructor seam, add one — same shape as
GoldBuilder(llm_judge=, retriever=, db_path=). Then construct it in the
test with a Fake* from tests/fakes.py.

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...

Forbidden example:
  @patch('kairix.core.search.bm25.bm25_search')
  def test_search_returns_hits(mock_search): ...

Stdlib boundaries (os.*, builtins.*) and external SDK boundaries
(openai.*, httpx.*) remain allowed — F1 only blocks kairix.* targets."

# Delegate to AST-based detector — a grep-based check would miss
# multi-line patch() invocations.
python3 "${SCRIPT_DIR}/check_no_internal_patches.py" \
    | arch_gate "no-internal-patches" "$REMEDIATION"
