"""F47: integration tests construct pipelines via the factory, not directly.

Tests under ``tests/integration/`` that exercise a multi-component
pipeline (``SearchPipeline``, ``EmbedPipeline``, ``ConnectorPipeline``,
``IngestPipeline``, or any other ``*Pipeline`` class imported from a
``kairix.*`` module) must construct it via
``kairix.core.factory.build_*`` with ``paths=FakePaths(...)`` and any
other injection seams the factory exposes.

Direct construction is allowed only in:
  * ``tests/contracts/`` — Protocol shape proofs that don't exercise
    composition (this detector does not scan there).
  * ``tests/integration/test_*_contract.py`` — single-layer boundary
    proofs that intentionally bypass composition.

Mechanical detection (AST):
  1. Walk ``tests/integration/test_*.py`` (skip ``*_contract.py``).
  2. For each file, collect imported class names whose name ends with
     ``Pipeline`` and whose source module starts with ``kairix.``.
  3. Flag the file when an ``ast.Call`` invokes a class name from step
     2 directly (call expression of the class name).

Per-file granularity — keeps violation reporting file-based, consistent
with every other F-rule. There is no grandfathering: every violation fails the gate.

Spec: ``docs/architecture/test-discipline-hardening.md`` §3 (F47).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import REPO_ROOT, repo_relative  # noqa: F401 — back-compat for collect_violations callers

REMEDIATION = """F47: tests/integration/<file>.py constructs <Pipeline> directly.

fix: use kairix.core.factory.build_<pipeline>(paths=FakePaths(...)).
next: see tests/integration/test_vec_index_lifecycle.py for the canonical
pattern, and docs/architecture/test-discipline-hardening.md §4.2.
run: bash scripts/checks/check-f47-integration-factory.sh

Pass example:

    from kairix.core.factory import build_search_pipeline
    from tests.fakes import FakePaths

    def test_boost_ordering(tmp_path, fixture_corpus_at):
        paths = FakePaths(root=tmp_path).with_corpus(fixture_corpus_at("reflib"))
        pipeline = build_search_pipeline(paths=paths)
        result = pipeline.run("query")
        assert result.documents[0].chunk_date_boost > 0

Forbidden example:

    from kairix.core.search.pipeline import SearchPipeline

    def test_boost_ordering(db, fixture_corpus):
        pipeline = SearchPipeline(           # direct construction — F47 violation
            document_repository=DocumentRepository(db),
            vector_repository=VectorRepository(db),
            ...
        )
        result = pipeline.run("query")

Allowed exceptions (not scanned / not flagged by this detector):
  * tests/contracts/ — Protocol shape proofs.
  * tests/integration/test_<x>_contract.py — single-layer boundary
    proofs that intentionally bypass composition.
"""


def _collect_pipeline_imports(tree: ast.Module) -> set[str]:
    """Return the set of names imported as ``*Pipeline`` from kairix.* modules.

    Two import shapes are handled:
      * ``from kairix.core.search.pipeline import SearchPipeline`` —
        ``SearchPipeline`` becomes a flagged name.
      * ``from kairix.core.search.pipeline import SearchPipeline as SP``
        — ``SP`` (the binding name) becomes the flagged name.

    Module-style imports (``import kairix.core.search.pipeline``) and
    attribute-access constructions (``pipeline.SearchPipeline(...)``)
    are intentionally NOT flagged: the factory remediation targets the
    common direct-name shape, and the few module-style usages in the
    tree (if any) are caught by code review.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module.startswith("kairix."):
                continue
            for alias in node.names:
                source_name = alias.name
                bound_name = alias.asname or alias.name
                if source_name.endswith("Pipeline"):
                    names.add(bound_name)
    return names


def _has_direct_construction(tree: ast.Module, pipeline_names: set[str]) -> bool:
    """Return True when any ``Call`` node invokes one of ``pipeline_names``.

    Only direct ``Name`` calls qualify — ``pipeline_module.SearchPipeline(...)``
    (an ``Attribute`` callee) is out of scope. The narrow surface keeps
    the detector predictable and matches the F47 spec's "call expression
    of the class name" wording.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in pipeline_names:
                return True
    return False


def file_violates(path: Path) -> bool:
    """Return True when ``path`` is a flagged integration test file.

    Exclusion rules applied before AST parse:
      * Filename must match ``test_*.py``.
      * Filename ending in ``_contract.py`` is exempt (single-layer
        boundary proof).
    """
    name = path.name
    if not name.startswith("test_") or not name.endswith(".py"):
        return False
    if name.endswith("_contract.py"):
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    pipeline_names = _collect_pipeline_imports(tree)
    if not pipeline_names:
        return False
    return _has_direct_construction(tree, pipeline_names)


def collect_violations(repo_root: Path) -> set[Path]:
    """Walk ``tests/integration/`` under ``repo_root``; return relative violation paths."""
    out: set[Path] = set()
    root = repo_root / "tests" / "integration"
    if not root.is_dir():
        return out
    for path in root.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        if file_violates(path):
            out.add(path.relative_to(repo_root))
    return out


class F47(FitnessRule):
    """F47 as a FitnessRule subclass — see module docstring.

    Scope: ``tests/integration/`` only. ``file_has_violation`` further
    filters to ``test_*.py`` files (skipping ``*_contract.py``).
    """

    name = "f47-integration-factory"
    remediation = REMEDIATION
    roots = ("tests/integration",)

    def file_has_violation(self, path: Path) -> bool:
        return file_violates(path)


def main() -> int:
    return F47().run()


if __name__ == "__main__":
    sys.exit(main())
