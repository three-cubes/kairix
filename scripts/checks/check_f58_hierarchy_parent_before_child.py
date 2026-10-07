"""F58: HierarchyNode parent-before-child invariant has a contract test.

ADR v2 (``docs/architecture/connector-scope-topology/ADR.md``) introduces
``HierarchyConnector.iter_containers()`` which emits ``HierarchyNode``
records. Each node has ``raw_parent_id`` either ``None`` (root) or
referencing a previously-emitted node within the same
``iter_containers()`` call. Out-of-order emission produces orphan
records — the hierarchy reconstruction layer either drops them or stores
forward-references that explode at query time.

This is a runtime invariant (not statically AST-checkable on production
code), so F58 makes it a TEST-collecting check: at least one test under
``tests/contracts/`` must:

  1. Have a function name matching ``test_*hierarchy*parent_before_child*``.
  2. Reference ``HierarchyConnector`` in its source (constructed
     directly, or via a Fake/Protocol type annotation).

When a Wave E ``HierarchyConnector`` plugin ships without the contract
test, F58 fires.

Phase A (today / Wave A): vacuous — no ``HierarchyConnector`` Protocol
or implementations exist yet, and no candidate test exists. We treat
the test as REQUIRED only when the production Protocol exists. Until
then the gate is green.

Phase B (Wave E onwards): the moment a class named ``HierarchyConnector``
appears in production code, F58 requires the contract test to exist.

There is no grandfathering: every violation fails the gate.

Per F21, ``REMEDIATION`` carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# The Protocol/class name whose presence in production code triggers
# the test requirement.
_HIERARCHY_SYMBOL = "HierarchyConnector"

# Test function names that satisfy F58. Matches e.g.
# test_hierarchy_parent_before_child,
# test_iter_containers_hierarchy_parent_before_child_invariant,
# etc.
_TEST_NAME_RE = re.compile(r"^test_.*hierarchy.*parent_before_child.*$", re.IGNORECASE)

# Marker for "violation": when the Protocol exists but no test is found,
# we report a synthetic path so the gate has something to point at.
_MISSING_TEST_MARKER = Path("tests/contracts/test_hierarchy_emission.py:MISSING")

REMEDIATION = """F58: HierarchyConnector exists in production code but no contract
test asserts the parent-before-child invariant.

Every HierarchyNode emission must have raw_parent_id either None (root) or
referencing a previously-emitted node within the same iter_containers() call.
Out-of-order emission silently produces orphan records.

fix: add a test under tests/contracts/ whose name matches
     'test_*hierarchy*parent_before_child*' AND references HierarchyConnector.
     The test should construct a FakeHierarchyConnector (per the canonical
     fakes inventory in 10-test-architecture.md) and assert that every
     yielded HierarchyNode satisfies the invariant. Sabotage-prove by
     mutating the connector to emit a child before its parent and watching
     the assertion fail.
next: see docs/architecture/connector-scope-topology/10-test-architecture.md
     §"New F-rules required" (F58) and §"Sabotage matrix" (HierarchyNode invariant).
run: python3 scripts/checks/check_f58_hierarchy_parent_before_child.py

Pass example:
  # tests/contracts/test_hierarchy_emission.py
  import pytest
  from tests.fakes import FakeHierarchyConnector
  from kairix.core.protocols import HierarchyConnector, HierarchyNode

  pytestmark = pytest.mark.contract

  def test_hierarchy_parent_before_child_invariant() -> None:
      connector: HierarchyConnector = FakeHierarchyConnector(
          nodes=[
              HierarchyNode(raw_id="root", raw_parent_id=None, name="Root"),
              HierarchyNode(raw_id="child", raw_parent_id="root", name="Child"),
          ],
      )
      seen: set[str] = set()
      for node in connector.iter_containers():
          if node.raw_parent_id is not None:
              assert node.raw_parent_id in seen, f"orphan emission: {node.raw_id}"
          seen.add(node.raw_id)

Forbidden example:
  # production code adds class HierarchyConnector(Protocol): ...
  # tests/contracts/ has no test_*hierarchy*parent_before_child*  →  F58 fires"""


def _production_defines_hierarchy_connector(repo_root: Path) -> bool:
    """True if any ``kairix/**/*.py`` file defines a class (or Protocol)
    named ``HierarchyConnector``.

    This is the gate that flips F58 from vacuous-green to mandatory.
    Pre-Wave-B the Protocol doesn't exist and F58 stays green.
    """
    kairix_dir = repo_root / "kairix"
    if not kairix_dir.exists():
        return False
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == _HIERARCHY_SYMBOL:
                return True
    return False


def _test_satisfies_f58(path: Path) -> bool:
    """True if ``path`` contains a test function whose name matches the
    F58 pattern AND whose source references ``HierarchyConnector``.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    # Cheap pre-filter: the file must mention HierarchyConnector somewhere.
    if _HIERARCHY_SYMBOL not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _TEST_NAME_RE.match(node.name):
            return True
    return False


def _contract_test_exists(repo_root: Path) -> bool:
    """True if any test under ``tests/contracts/`` satisfies F58."""
    contracts_dir = repo_root / "tests" / "contracts"
    if not contracts_dir.exists():
        return False
    for path in contracts_dir.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        if _test_satisfies_f58(path):
            return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Return ``{Path}`` containing a synthetic missing-test marker when
    HierarchyConnector exists in production code but no satisfying test
    is found under ``tests/contracts/``. Otherwise return empty set
    (vacuous-green pre-Wave-B; clean post-Wave-B with the test landed).
    """
    if not _production_defines_hierarchy_connector(repo_root):
        return set()
    if _contract_test_exists(repo_root):
        return set()
    return {_MISSING_TEST_MARKER}


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 on net-new violations."""
    return gate("f58", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
