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

# Delegate to AST-based detector (resolves #214 — grep missed multi-line patch()).
python3 "${SCRIPT_DIR}/check_no_internal_patches.py" \
    | arch_gate "no-internal-patches" "$REMEDIATION"
