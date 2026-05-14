"""F1 detector: no @patch / with patch on kairix internals.

Walks every test file via AST and emits the path of any file that uses
``@patch("kairix.X.Y", ...)`` or ``with patch("kairix.X.Y", ...):`` as a
decorator/context manager, regardless of line layout.

Resolves #214 (multi-line ``with patch(\\n  "kairix...")`` escaped the
prior grep-based detector).

Output: one violation file path per line on stdout, sorted, deduplicated.
Pipes into ``arch_gate`` from ``_lib.sh`` for baseline diff.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# REMEDIATION text — the shell wrapper ``check-no-internal-patches.sh``
# owns the user-facing message that prints when the gate fails. This
# constant exists for F21 (actionable-feedback) compliance and is
# semantically equivalent to the shell wrapper's REMEDIATION.
REMEDIATION = """Refactor to constructor injection with a fake from tests/fakes.py
(no @patch / with patch on kairix.* targets) — to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. ``SearchPipeline(retriever=FakeRetriever(...))``)
instead of patching the internal symbol. If the production class
lacks a constructor seam, add one — same shape as
``GoldBuilder(llm_judge=, retriever=, db_path=)``.
next: re-run ``python3 scripts/checks/check_no_internal_patches.py``
(or ``bash scripts/checks/check-no-internal-patches.sh``) to confirm
the gate goes green.
run: bash scripts/safe-commit.sh "test(<area>): inject fake instead of patching internals"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...

Forbidden example:
  @patch('kairix.core.search.bm25.bm25_search')
  def test_search_returns_hits(mock_search): ...

Stdlib boundaries (os.*, builtins.*) and external SDK boundaries
(openai.*, httpx.*) remain allowed — F1 only blocks kairix.* targets."""


def _is_patch_call(node: ast.expr) -> bool:
    """Return True when ``node`` is a Call to ``patch`` or ``mock.patch`` /
    ``unittest.mock.patch`` (or aliases like ``mock_patch``).

    Conservative: only matches the literal name ``patch`` or attribute
    access ending in ``.patch``. Other helpers (``patch.dict``,
    ``patch.object``) are NOT covered by F1 — they have a different
    arg shape and are tracked separately if needed.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _first_arg_is_kairix_internal(call: ast.Call) -> bool:
    """First positional arg of patch(...) is a string starting with ``kairix.``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value.startswith("kairix.")
    return False


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains @patch or with patch on a kairix.* target."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    for node in ast.walk(tree):
        # Decorator: @patch("kairix.X.Y")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                if _is_patch_call(deco) and _first_arg_is_kairix_internal(deco):
                    return True
        # Context manager: with patch("kairix.X.Y"): ...
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if _is_patch_call(ctx) and _first_arg_is_kairix_internal(ctx):
                    return True
    return False


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    violators: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if file_has_internal_patch(path):
            violators.append(str(path))

    for v in violators:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
