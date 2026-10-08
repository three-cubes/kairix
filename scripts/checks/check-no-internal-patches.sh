#!/usr/bin/env bash
# F1: No @patch / sys.modules swap / importlib.reload on kairix internal code.
#
# Tests must not patch kairix.* — refactor to use constructor injection or a
# Protocol seam from kairix.core.protocols. Stdlib boundaries (os.*, builtins.*)
# and external SDK boundaries (openai.*, httpx.*) remain allowed.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="kairix-internal substitution found in a test (@patch / monkeypatch.setattr /
attribute assignment on a kairix target, a sys.modules swap of a kairix
module, or importlib.reload of one). Refactor to constructor injection
with a fake from tests/fakes.py to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. SearchPipeline(retriever=FakeRetriever(...)))
instead of patching the internal symbol. If the production class
lacks a constructor seam, add one — same shape as
GoldBuilder(llm_judge=, retriever=, db_path=). A sys.modules swap or
importlib.reload usually resets module-level singleton state or fakes an
import failure: move the state onto an injectable holder (CrossEncoderCache
in kairix/core/search/rerank.py) or the import onto a Deps seam
(PackageInitDeps.import_module in kairix/package_meta.py). To prove an
import has no side effects, import it in a fresh interpreter
(subprocess.run([sys.executable, '-c', 'import X'])) instead of reloading.
next: re-run 'bash scripts/checks/check-no-internal-patches.sh' to
confirm the gate goes green.
run: bash scripts/safe-commit.sh \"test(<area>): inject fake instead of patching internals\"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...
  assert get_cross_encoder('m', cache=CrossEncoderCache()) is None

Forbidden example:
  @patch('kairix.core.search.bm25.bm25_search')
  def test_search_returns_hits(mock_search): ...
  sys.modules['kairix.core.search.pipeline'] = BrokenModule(...)
  importlib.reload(kairix.core.search.rerank)

Stdlib boundaries (os.*, builtins.*) and external SDK boundaries
(openai.*, httpx.*, sys.modules['openai']) remain allowed — F1 only
blocks kairix.* targets."

# Delegate to AST-based detector — a grep-based check would miss
# multi-line patch() invocations.
python3 "${SCRIPT_DIR}/check_no_internal_patches.py" \
    | arch_gate "no-internal-patches" "$REMEDIATION"
