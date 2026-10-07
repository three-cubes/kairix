"""Declarative table engine for the location / singleton fitness rules.

Three rules (F29, F38, F61) all walk ``kairix/**`` and flag a file whose
*shape* — its filename, a ``def`` name it contains, or a constructor call it
makes — appears outside an allowed set of roots. Before #499 Phase 2 each
shipped its own ~150-line ``check_*.py`` re-implementing the same
walk-and-gate skeleton. This engine collapses that into one row schema + one
walker; each ``check_*.py`` becomes a thin shim that looks its row up and
re-exports the back-compat surface its unit tests call.

The :class:`LocationRule` row is the single point of variation. Its ``kind``
discriminator selects one of three detection shapes:

* ``"filename-regex"`` — flag a file whose *basename* matches ``pattern``
  (F29's bench / latency / perf naming). F29 additionally permits
  ``scripts/probe*.{py,sh}`` operational drivers; that special allowance is
  carried by the ``probe_scripts`` flag.

* ``"def-name"`` — AST-walk and flag a file defining at least one ``def`` /
  ``async def`` whose name matches ``pattern`` (F38's chunk_* / _chunk* /
  tokenize_into_chunks). ``allowed_files`` carries the single-file exemption
  (``kairix/core/connectors/silver.py``).

* ``"ctor-call"`` — AST-walk and flag a file making a bare ``pattern(...)``
  constructor call by ``ast.Name`` (F61's ``_SqliteChunkWriter(...)``).
  Attribute-form ``mod.X(...)`` is intentionally out of scope (covered by the
  import-boundary rules).

Every rule's scan is scoped to ``root/kairix`` — matching the original free
``collect_violations`` functions, which walked ``kairix/`` only even though a
few allow-list entries (``tests/``, ``scripts/probe*``) name trees outside it.
Those entries stay in the allow-list for completeness but never fire because
the walk never reaches them.

``collect_violations_for(rule, root)`` returns a set of repo-relative
:class:`~pathlib.Path` objects — the exact shape the old detectors returned,
so the per-rule unit tests pass unchanged.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT

Kind = Literal["filename-regex", "def-name", "ctor-call"]

# scripts/probe*.{py,sh} operational drivers — F29's one allow-list entry
# outside the prefix list. Compiled once, consulted only by F29.
_PROBE_SCRIPT_RE = re.compile(r"^probe[a-z0-9_-]*\.(py|sh)$")


@dataclass(frozen=True)
class LocationRule:
    """One location / singleton rule, expressed declaratively.

    Fields:
        name: gate name used in the verdict line (e.g. ``"f29"`` →
            ``FAIL [arch:f29]``).
        kind: detection discriminator — ``"filename-regex"`` / ``"def-name"``
            / ``"ctor-call"``.
        pattern: compiled regex (filename / def-name kinds) or the literal
            constructor name (ctor-call kind).
        allowed_roots: repo-relative directory prefixes under which the shape
            is permitted.
        remediation: F21-compliant fix/next/run remediation text.
        allowed_files: repo-relative file paths (not prefixes) that are
            exempt — F38's single ``silver.py`` home.
        probe_scripts: F29's extra ``scripts/probe*`` allowance.
    """

    name: str
    kind: Kind
    pattern: re.Pattern[str] | str
    allowed_roots: tuple[str, ...]
    remediation: str
    allowed_files: tuple[str, ...] = field(default_factory=tuple)
    probe_scripts: bool = False


# ── shape detectors (one per kind) ───────────────────────────────────────


def _matches_filename(rule: LocationRule, path: Path) -> bool:
    """``filename-regex`` kind: True if ``path``'s basename matches the pattern."""
    assert isinstance(rule.pattern, re.Pattern)
    return rule.pattern.match(path.name) is not None


def _parse(path: Path) -> ast.AST | None:
    """Parse ``path`` to an AST, or ``None`` on read / syntax error."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def _defines_matching_function(rule: LocationRule, path: Path) -> bool:
    """``def-name`` kind: True if ``path`` defines a function (sync or async,
    module-level or nested) whose name matches the pattern."""
    assert isinstance(rule.pattern, re.Pattern)
    tree = _parse(path)
    if tree is None:
        return False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and rule.pattern.match(node.name):
            return True
    return False


def _constructs_matching_call(rule: LocationRule, path: Path) -> bool:
    """``ctor-call`` kind: True if ``path`` makes a bare ``pattern(...)``
    constructor call (an ``ast.Call`` whose ``func`` is an ``ast.Name`` equal
    to the pattern). A cheap substring pre-filter skips the AST parse when the
    name isn't even mentioned."""
    assert isinstance(rule.pattern, str)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if rule.pattern not in source:
        return False
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == rule.pattern:
            return True
    return False


_SHAPE_DETECTORS = {
    "filename-regex": _matches_filename,
    "def-name": _defines_matching_function,
    "ctor-call": _constructs_matching_call,
}


# ── allow-list ───────────────────────────────────────────────────────────


def _under_prefix(rel: Path, prefix: str) -> bool:
    """True if repo-relative ``rel`` sits under directory ``prefix``."""
    prefix_parts = Path(prefix).parts
    parts = rel.parts
    return len(parts) >= len(prefix_parts) and tuple(parts[: len(prefix_parts)]) == prefix_parts


def _is_allowed(rule: LocationRule, rel: Path) -> bool:
    """True if a flagged-shape file at repo-relative ``rel`` is permitted by
    ``rule``'s allow-list (a prefix root, an exact file, or — for F29 — a
    ``scripts/probe*`` driver)."""
    if str(rel) in rule.allowed_files:
        return True
    if any(_under_prefix(rel, prefix) for prefix in rule.allowed_roots):
        return True
    if rule.probe_scripts:
        parts = rel.parts
        if len(parts) >= 2 and parts[0] == "scripts" and _PROBE_SCRIPT_RE.match(parts[-1]):
            return True
    return False


# ── enumeration + dispatch ───────────────────────────────────────────────


def collect_violations_for(rule: LocationRule, root: Path = REPO_ROOT) -> set[Path]:
    """Walk every ``.py`` file under ``root/kairix`` and return the set of
    repo-relative paths whose shape matches ``rule`` AND that live outside the
    allow-list.

    Scoped to ``kairix/`` — the production package — exactly as the original
    detectors were. Files under ``tests/`` are out of scope by construction
    (never reached by the walk).
    """
    kairix_dir = root / "kairix"
    if not kairix_dir.exists():
        return set()

    detector = _SHAPE_DETECTORS[rule.kind]
    violations: set[Path] = set()
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if not detector(rule, path):
            continue
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        if _is_allowed(rule, rel):
            continue
        violations.add(rel)
    return violations


# ── the rule table ───────────────────────────────────────────────────────
#
# Remediation text is authored in each shim (the unit tests read it back via
# ``detector.REMEDIATION``) and injected here via :func:`register`, keeping
# the engine remediation-agnostic while the table stays the single lookup
# surface.

_RULES: dict[str, LocationRule] = {}


def register(rule: LocationRule) -> LocationRule:
    """Register ``rule`` keyed by ``rule.name`` and return it."""
    _RULES[rule.name] = rule
    return rule


def rule_for(name: str) -> LocationRule:
    """Return the registered :class:`LocationRule` named ``name``."""
    return _RULES[name]
