"""F55: every Chunker plugin declares ``version: str`` AND every Chunk write
passes ``chunker_version=``.

Mirrors F40 (extractor version) for the new ``Chunker`` registry layer that
ADR v2 (``docs/architecture/connector-scope-topology/ADR.md``) introduces.
ADR v2 §"Table B" rationale: when a chunker bumps a version
(``MarkdownStructuralChunker`` 1 → 2 changes paragraph boundaries), the
``documents_media.chunker_version`` column tells us which chunks need
re-chunking. Without the version surfaced at the write site, re-chunk
sweeps become whole-corpus rebuilds.

Two-part detection:

1. **Chunker plugin declares ``version``** — every directory under
   ``kairix/chunkers/<name>/`` (excluding ``_``-prefixed module stubs)
   must have an ``__init__.py`` containing a module-level
   ``version: str = "..."`` (annotated or bare) with a non-empty string
   literal.
2. **Every ``Chunk(...)`` constructor call passes ``chunker_version=``** —
   AST-walk every ``kairix/**/*.py`` file and flag any ``Chunk(name=...)``
   call that omits the kwarg. A ``**kwargs`` splat is conservatively
   accepted (the runtime check happens at the dataclass boundary).

Phase A (today / Wave A): vacuous — ``kairix/chunkers/`` does not exist
and the only existing ``Chunk(...)`` callsites are in
``kairix/core/connectors/silver.py``, outside the Phase A scope until
Wave C threads ``chunker_version`` through Silver.

Phase B (Wave F): every chunker plugin declares ``version: str`` AND
every ``Chunk(...)`` call in ``kairix/chunkers/<name>/__init__.py`` (or
in code that constructs Chunks on behalf of a chunker) passes
``chunker_version=self.version`` (or equivalent). Baseline shrinks to
zero as Silver rewires through the registry.

Per F21, ``REMEDIATION`` carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

# Matches the bare ``Chunk(...)`` constructor (same shape as F39 — does
# not match ``module.Chunk(...)`` attribute access, ``TemporalChunk``,
# ``ChunkDateBoost``, or other prefixed shapes).
_CHUNK_CTOR_NAMES: frozenset[str] = frozenset({"Chunk"})

REMEDIATION = """F55: Chunker plugin missing version, or Chunk(...) write missing chunker_version kwarg.

Every Chunker plugin must declare a module-level 'version: str' (mirrors F40 for extractors)
AND every Chunk(...) write must pass chunker_version=<chunker.version> so a chunker bump
triggers tractable re-chunking via documents_media.chunker_version.

fix: declare 'version: str = "<semver-or-date>"' in kairix/chunkers/<name>/__init__.py.
     At every Chunk(...) construction site, pass chunker_version=<chunker.version>.
next: see docs/architecture/connector-scope-topology/ADR.md §"Table B"
     (chunker registry) + 10-test-architecture.md §"New F-rules required" (F55).
run: python3 scripts/checks/check_f55_chunker_version.py

Pass example:
  # kairix/chunkers/markdown_structural/__init__.py
  from __future__ import annotations

  version: str = "2"

  def make_chunker() -> Chunker:
      return MarkdownStructuralChunker(version=version)

  # at the Chunk(...) write site:
  Chunk(
      text=segment,
      content_hash=h,
      source_name=connector.name,
      source_uri=uri,
      source_modified_at=mtime,
      sensitivity=sens,
      chunker_version=self.version,   # <- F55
  )

Forbidden example:
  # kairix/chunkers/markdown_structural/__init__.py — F55 fires (no version)
  def make_chunker() -> Chunker:
      return MarkdownStructuralChunker()

  Chunk(text=t, content_hash=h, source_name=n, source_uri=u,
        source_modified_at=m, sensitivity=s)
  # F55 fires — no chunker_version kwarg, re-chunk sweeps can't filter."""


def _is_version_assignment(node: ast.stmt) -> bool:
    """True if ``node`` is a module-level ``version: str = "..."`` or
    ``version = "..."`` with a non-empty string-literal RHS.

    Mirrors F40's ``_is_version_assignment``; same intent — "the version
    is declared at the module surface".
    """
    if isinstance(node, ast.AnnAssign):
        if not (isinstance(node.target, ast.Name) and node.target.id == "version"):
            return False
        value = node.value
    elif isinstance(node, ast.Assign):
        if not (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "version"):
            return False
        value = node.value
    else:
        return False
    return isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip() != ""


def _has_version_declaration(tree: ast.Module) -> bool:
    """True if the module defines a top-level non-empty ``version: str``."""
    return any(_is_version_assignment(node) for node in tree.body)


def _is_chunker_plugin_dir(path: Path) -> bool:
    """A chunker plugin directory is under ``kairix/chunkers/`` whose
    name does not start with ``_`` and contains an ``__init__.py``."""
    if not path.is_dir():
        return False
    if path.name.startswith("_") or path.name == "__pycache__":
        return False
    return (path / "__init__.py").exists()


def _plugin_missing_version(init_path: Path) -> bool:
    """True if the chunker plugin at ``init_path`` lacks a ``version`` declaration."""
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except (SyntaxError, UnicodeDecodeError):
        return True
    return not _has_version_declaration(tree)


def _is_chunk_ctor(call: ast.Call) -> bool:
    """True if ``call`` is a ``Chunk(...)`` constructor invocation.

    Bare ``ast.Name`` match only — same scope as F39.
    ``module.Chunk(...)`` (Attribute access) is NOT matched: a cross-
    module attribute reach already trips F26/F27.
    """
    return isinstance(call.func, ast.Name) and call.func.id in _CHUNK_CTOR_NAMES


def _chunk_call_missing_version(call: ast.Call) -> bool:
    """True if a ``Chunk(...)`` call omits the ``chunker_version=`` kwarg.

    A ``**kwargs`` splat (``ast.keyword`` with ``arg is None``) is
    conservatively treated as supplying the kwarg.
    """
    if any(kw.arg is None for kw in call.keywords):
        return False
    passed = {kw.arg for kw in call.keywords if kw.arg is not None}
    return "chunker_version" not in passed


def _file_has_chunk_call_violation(path: Path) -> bool:
    """True if any ``Chunk(...)`` call in this file omits ``chunker_version=``."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_chunk_ctor(node) and _chunk_call_missing_version(node):
            return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Return repo-relative paths that violate F55 — either a chunker
    plugin missing ``version`` OR a ``Chunk(...)`` call missing
    ``chunker_version=``.
    """
    violations: set[Path] = set()

    # Part 1 — chunker plugin missing version.
    chunkers_root = repo_root / "kairix" / "chunkers"
    if chunkers_root.exists():
        for entry in sorted(chunkers_root.iterdir()):
            if not _is_chunker_plugin_dir(entry):
                continue
            init_path = entry / "__init__.py"
            if _plugin_missing_version(init_path):
                violations.add(init_path.resolve().relative_to(repo_root))

    # Part 2 — Chunk(...) call without chunker_version kwarg.
    kairix_dir = repo_root / "kairix"
    if kairix_dir.exists():
        for path in kairix_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if _file_has_chunk_call_violation(path):
                try:
                    violations.add(path.resolve().relative_to(repo_root))
                except ValueError:
                    continue

    return violations


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 on net-new violations."""
    return gate("f55", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
