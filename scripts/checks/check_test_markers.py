"""F8: Every test function has a category marker.

Per CLAUDE.md and ``[tool.pytest.ini_options]`` in pyproject.toml, every
test function must declare its category via a marker so the
test-pyramid filter (``pytest -m unit``) is meaningful. Missing
markers degrade the pyramid into "everything runs together," which
makes contract-only or unit-only runs unreliable.

A test function passes when AT LEAST ONE of:

  - The function carries ``@pytest.mark.<category>``.
  - The enclosing class assigns ``pytestmark = pytest.mark.<category>``
    (or a list including one).
  - The module assigns ``pytestmark = pytest.mark.<category>``
    (or a list including one).

Recognised category markers (from pyproject.toml's marker list):
``unit``, ``bdd``, ``contract``, ``integration``, ``e2e``, ``slow``.

The check ignores fixtures (``@pytest.fixture``-decorated), step
definitions in ``tests/bdd/steps/`` (no ``def test_*`` functions
there), and helper functions whose names don't start with ``test_``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate, python_files, repo_relative

# Category markers from pyproject.toml's `[tool.pytest.ini_options].markers`.
KNOWN_MARKERS = frozenset({"unit", "bdd", "contract", "integration", "e2e", "slow"})

REMEDIATION = """Refactor to add ONE category marker per test
(function-level decorator, class-level decorator, or module-level
``pytestmark`` assignment) to pass.

fix: add ``pytestmark = pytest.mark.unit`` (or the appropriate category)
at the top of the listed test file — that covers every test in the
module in one line. For mixed-category files use per-function
``@pytest.mark.<category>`` decorators instead.
next: re-run ``python3 scripts/checks/check_test_markers.py`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "test(<area>): add category marker to <file>"

Recognised markers (per pyproject.toml): unit, bdd, contract, integration,
e2e, slow.

Pass example:
  # module-level (covers every test in the file)
  pytestmark = pytest.mark.unit

  def test_search_returns_hits():
      ...

  # OR function-level
  @pytest.mark.unit
  def test_search_returns_hits():
      ...

  # OR list form at class level
  class TestSearch:
      pytestmark = [pytest.mark.bdd]
      def test_one(self): ...

Forbidden example:
  def test_search_returns_hits():  # no marker anywhere — fails F8
      ...

Why: the test-pyramid filter (``pytest -m unit``) is only meaningful when
every test declares its category. Unmarked tests run in every selection,
defeating the pyramid."""


def _decorator_is_category_marker(node: ast.expr) -> bool:
    """True if ``node`` is ``@pytest.mark.<category>`` or
    ``@pytest.mark.<category>(...)`` for a known category.
    """
    # @pytest.mark.parametrize → ast.Call wrapping ast.Attribute
    if isinstance(node, ast.Call):
        return _decorator_is_category_marker(node.func)

    # @pytest.mark.unit → ast.Attribute(value=Attribute(value=Name('pytest'), attr='mark'), attr='unit')
    if isinstance(node, ast.Attribute):
        if node.attr in KNOWN_MARKERS:
            inner = node.value
            if isinstance(inner, ast.Attribute) and inner.attr == "mark":
                return True
            # Also accept `from pytest import mark` → @mark.unit
            if isinstance(inner, ast.Name) and inner.id == "mark":
                return True
    return False


def _expression_carries_category_marker(node: ast.expr) -> bool:
    """True if ``node`` is a category marker reference suitable as a
    ``pytestmark`` value: ``pytest.mark.unit``, ``mark.unit``, or a
    list containing one.
    """
    if _decorator_is_category_marker(node):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_expression_carries_category_marker(elt) for elt in node.elts)
    return False


def _scope_has_pytestmark(body: list[ast.stmt]) -> bool:
    """True if the given module-or-class body assigns a category-marker
    to ``pytestmark``.
    """
    for stmt in body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "pytestmark":
                    if _expression_carries_category_marker(stmt.value):
                        return True
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == "pytestmark":
                if stmt.value is not None and _expression_carries_category_marker(stmt.value):
                    return True
    return False


def _function_has_marker(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(_decorator_is_category_marker(d) for d in func.decorator_list)


def _is_pytest_fixture(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function is decorated with ``@pytest.fixture`` (or
    ``@pytest.fixture(...)``). Fixtures named ``test_*`` are still
    fixtures — pytest distinguishes by decorator, not by name.
    """
    for d in func.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


def _is_pytest_test(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if pytest would collect this function as a test: name starts
    with ``test_`` AND it is not decorated as a fixture.
    """
    return func.name.startswith("test_") and not _is_pytest_fixture(func)


def file_has_violation(path: Path) -> bool:
    """True if any top-level or class-level ``def test_*`` in this file
    lacks a category marker (function-level, class-level pytestmark, or
    module-level pytestmark). Fixtures named ``test_*`` are excluded.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    if _scope_has_pytestmark(tree.body):
        return False

    for top_level in tree.body:
        if isinstance(top_level, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_pytest_test(top_level) and not _function_has_marker(top_level):
                return True

        elif isinstance(top_level, ast.ClassDef):
            # Class is "marked" if it has a class-level pytestmark
            # assignment OR a class-level @pytest.mark.<category>
            # decorator (which pytest applies to every test method).
            class_marked = _scope_has_pytestmark(top_level.body) or any(
                _decorator_is_category_marker(d) for d in top_level.decorator_list
            )
            for member in top_level.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_pytest_test(member) and not class_marked and not _function_has_marker(member):
                        return True

    return False


def main() -> int:
    violations = {repo_relative(p) for p in python_files("tests") if file_has_violation(p)}
    return gate("test-markers", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
