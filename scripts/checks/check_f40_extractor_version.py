"""F40: Every Extractor plugin declares ``version: str`` and surfaces it.

The connector-ingestion ADR (``docs/architecture/connector-ingestion-architecture.md``)
§5.6 names the schema-drift failure mode: a new version of markitdown
emits different markdown for the same PDF, the old derivatives are
stale, and there's no way to identify them for re-extraction. F40
forces every ``Extractor`` plugin under ``kairix/extractors/<name>/``
to declare a non-empty ``version: str`` module-level attribute that
gets written through to ``documents_media.extractor_version`` when
the extractor produces an ``ExtractedDocument``. Re-extracts become
tractable (``kairix derivatives re-extract --extractor=markitdown
--since=<version>``).

Detection (AST):

1. For every directory ``kairix/extractors/<name>/`` (excluding
   ``_base.py`` / the underscore-prefixed registry stubs), require an
   ``__init__.py``.
2. The ``__init__.py`` must contain a module-level ``version: str =
   "..."`` assignment with a non-empty string literal — or an
   ``ast.AnnAssign`` of the same shape, or a plain ``ast.Assign`` with
   the name ``version`` and a non-empty string-literal value.
3. The ``__init__.py`` must also contain a top-level ``def
   make_extractor(...)`` function — the entry-point factory that
   ``connector-ingestion-architecture.md`` §8 commits to.

Today: no extractors exist (``kairix/extractors/`` directory absent).
The check is **vacuous-green** — no plugin directories → no violations.
A Wave 1 commit landing ``kairix/extractors/markitdown/__init__.py``
without a ``version`` declaration immediately triggers the rule.

There is no grandfathering: every violation blocks at pre-commit and CI.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import REPO_ROOT, repo_relative

# Root containing every Extractor plugin. ``_base.py`` / underscore-
# prefixed module stubs are not plugins.
_EXTRACTORS_ROOT = REPO_ROOT / "kairix" / "extractors"

REMEDIATION = """F40: an Extractor plugin is missing a module-level version: str declaration
(or its make_extractor factory function).

Every extractor must surface a version so derivatives stay tractable —
when markitdown / pdf_fallback / OCR / vision bumps a version, the
'documents_media.extractor_version' column tells us which chunks need
re-extracting.

fix: declare 'version: str = "<semver-or-date>"' in kairix/extractors/<name>/__init__.py.
next: see §3 (Extractor Protocol).
run: bash scripts/safe-commit.sh "feat(extractors/<name>): declare version"

Pass example:
  # kairix/extractors/markitdown/__init__.py
  from __future__ import annotations

  version: str = "0.1.4"

  def make_extractor() -> Extractor:
      return MarkItDownExtractor(version=version)

Forbidden example:
  # kairix/extractors/markitdown/__init__.py  — F40 fires
  from __future__ import annotations

  def make_extractor() -> Extractor:
      return MarkItDownExtractor()  # no version surfaced

The version is what the schema's documents_media.extractor_version
column records per write. Plugins MUST NOT omit it; a re-extract
sweep needs the version comparison to fire."""


def _is_version_assignment(node: ast.stmt) -> bool:
    """True if ``node`` is a module-level ``version: str = "..."`` or
    ``version = "..."`` assignment with a non-empty string-literal RHS.

    Both ``ast.AnnAssign`` (annotated) and ``ast.Assign`` (bare) shapes
    are accepted; the canonical form per the ADR is annotated, but the
    rule's intent is "the version is declared", not "the annotation is
    spelled".
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


def _has_make_extractor(tree: ast.Module) -> bool:
    """True if the module defines a top-level ``def make_extractor(...)``."""
    return any(isinstance(node, ast.FunctionDef) and node.name == "make_extractor" for node in tree.body)


def _has_version_declaration(tree: ast.Module) -> bool:
    """True if the module defines a top-level non-empty ``version: str``."""
    return any(_is_version_assignment(node) for node in tree.body)


def _is_plugin_dir(path: Path) -> bool:
    """A plugin directory is a directory under ``kairix/extractors/`` whose
    name does not start with ``_`` (skips ``_base.py``, ``__pycache__``,
    etc.) and that contains an ``__init__.py``.
    """
    if not path.is_dir():
        return False
    if path.name.startswith("_") or path.name == "__pycache__":
        return False
    return (path / "__init__.py").exists()


def init_has_violation(init_path: Path) -> bool:
    """True if the extractor plugin at ``init_path`` is missing either
    the ``version: str`` declaration or the ``make_extractor`` factory."""
    try:
        tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))
    except (SyntaxError, UnicodeDecodeError):
        return True  # unparseable plugin = treated as missing both — caller refactors
    return not (_has_version_declaration(tree) and _has_make_extractor(tree))


def collect_violations() -> set[Path]:
    """Walk ``kairix/extractors/`` and return repo-relative ``__init__.py``
    paths for plugin dirs that fail F40."""
    if not _EXTRACTORS_ROOT.exists():
        return set()
    violations: set[Path] = set()
    for entry in sorted(_EXTRACTORS_ROOT.iterdir()):
        if not _is_plugin_dir(entry):
            continue
        init_path = entry / "__init__.py"
        if init_has_violation(init_path):
            violations.add(repo_relative(init_path))
    return violations


class F40(FitnessRule):
    """F40 as a FitnessRule subclass — see module docstring.

    Overrides :meth:`enumerate_files` to yield ``__init__.py`` paths
    per Extractor plugin (the violation key is the plugin's
    ``__init__.py``, not its directory).
    """

    name = "f40"
    remediation = REMEDIATION
    roots = ("kairix/extractors",)

    def enumerate_files(self) -> list[Path]:
        extractors_root = self._repo_root / "kairix" / "extractors"
        if not extractors_root.exists():
            return []
        out: list[Path] = []
        for entry in sorted(extractors_root.iterdir()):
            if not _is_plugin_dir(entry):
                continue
            out.append(entry / "__init__.py")
        return out

    def is_in_scope(self, rel: str) -> bool:
        return True

    def file_has_violation(self, path: Path) -> bool:
        return init_has_violation(path)


def main() -> int:
    return F40().run()


if __name__ == "__main__":
    sys.exit(main())
