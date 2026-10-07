"""Declarative table engine for the import-boundary fitness rules.

Five rules (F26, F27, F34, F35, F44) and an inverse sixth (F37) all
AST-walk ``Import`` / ``ImportFrom`` nodes and gate on the module target.
Before #499 Phase 2 each shipped its own ~150-line ``check_*.py`` that
re-implemented the same walk with bespoke constants. This engine collapses
that into one row schema + one walker; each ``check_*.py`` is now a thin
shim that looks its row up by ``name`` and re-exports the back-compat
surface the per-rule unit tests call.

The :class:`ImportBoundaryRule` row is the single point of variation. Its
``mode`` discriminator selects one of three detection shapes:

* ``"prefix"`` — flag any import whose target equals or is dotted-under a
  forbidden prefix (F26, F34, F44). ``exempt`` carries the composition-root
  allowlist (F26's ``kairix/core/factory.py``).

* ``"sibling-plugin"`` — infer the plugin directory that owns the file
  (first path segment under ``roots[0]``) and flag any import that reaches a
  *different* plugin under the same tree. Underscore-prefixed heads (``_base``)
  are shared scaffolding, never cross-plugin. F35 additionally ORs the
  ``forbidden_prefixes`` predicate (any ``kairix.extractors`` reach).

* ``"sync-lib"`` — inverse. Flag any file that imports a third-party
  change-detection library (``forbidden_prefixes`` here are import-name roots
  like ``watchdog``) UNLESS the file lives under one of ``allowed_roots``.
  ``slack_sdk`` additionally requires an rtm / socket_mode submodule tail.

``collect_violations_for(rule, root)`` walks the in-scope tree and returns a
set of repo-relative :class:`~pathlib.Path` objects — the exact shape the old
free ``collect_violations`` functions returned, so the per-rule unit tests
(which load the shim by file path and call ``collect_violations(tmp_path)``)
pass unchanged.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT

Mode = Literal["prefix", "sibling-plugin", "sync-lib"]

# slack_sdk submodule tails that mark its real-time / socket-mode change
# detection surfaces. The bare SDK (slack_sdk.web) is a one-shot HTTP client
# and is NOT a sync loop, so it is not flagged.
_SLACK_ROOT = "slack_sdk"
_SLACK_SYNC_TAILS: frozenset[str] = frozenset({"rtm", "socket_mode"})


@dataclass(frozen=True)
class ImportBoundaryRule:
    """One import-boundary rule, expressed declaratively.

    Fields:
        name: gate name used in the verdict line (e.g. ``"f26"`` →
            ``FAIL [arch:f26]``).
        roots: repo-relative directory prefixes to scan (from-globs). For
            ``sibling-plugin`` mode, ``roots[0]`` is the plugin-tree root used
            to infer the owning plugin.
        mode: detection discriminator — ``"prefix"`` / ``"sibling-plugin"`` /
            ``"sync-lib"``.
        forbidden_prefixes: for ``prefix`` mode, the denied module prefixes;
            for ``sibling-plugin`` mode, an EXTRA forbidden-prefix predicate
            ORed onto the sibling check (F35's extractor ban); for
            ``sync-lib`` mode, the import-name roots that mark a sync library.
        exempt: repo-relative path globs whose files are never flagged
            (F26's composition root).
        allowed_roots: for ``sync-lib`` mode, the directory prefixes under
            which a forbidden import is permitted.
        remediation: F21-compliant fix/next/run remediation text.
    """

    name: str
    roots: tuple[str, ...]
    mode: Mode
    remediation: str
    forbidden_prefixes: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()
    allowed_roots: tuple[str, ...] = ()
    extra_forbidden_prefixes: tuple[str, ...] = field(default_factory=tuple)


# ── shared AST helpers ──────────────────────────────────────────────────


def _parse(path: Path) -> ast.AST | None:
    """Parse ``path`` to an AST, or return ``None`` on any read / syntax error.

    Mirrors the original detectors' tolerant behaviour: an unreadable or
    syntactically-broken file is simply not flagged (the broken file fails
    its own lint / type gate elsewhere).
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError:
        return None


def _import_modules(tree: ast.AST, *, skip_relative: bool = False) -> list[str]:
    """Every dotted module name referenced by an ``import`` / ``from ... import``.

    ``import a.b.c`` → ``"a.b.c"``; ``from a.b.c import x`` → ``"a.b.c"``.

    ``skip_relative`` selects the ``ImportFrom.level`` policy, which MUST match
    each original detector byte-for-behaviour (#499 Phase 2 faithfulness fix):

    * ``False`` (default) — yield ``node.module`` for EVERY ``ImportFrom``,
      regardless of ``node.level``. This is what the F26/F27/F34/F35/F44
      originals did: their ``file_has_violation`` / ``_imported_names`` /
      ``_plugin_dir_for`` walkers inspect ``node.module`` directly with no
      ``node.level`` guard, so a relative import whose ``.module`` matches a
      forbidden prefix / sibling plugin (``from .kairix.providers import x`` →
      level=1, module=``kairix.providers``; ``from ..psycopg2 import bar`` →
      level=2, module=``psycopg2``) IS flagged. Skipping ``node.level > 0``
      here silently dropped those — the divergence this fix closes.
    * ``True`` — skip any ``ImportFrom`` with ``node.level > 0``. This matches
      the F37 original ``_import_targets``, which alone guards ``node.level``
      (a relative import cannot reach a third-party sync library by
      construction, so it is correctly ignored there).

    ``module is None`` (``from . import x``) is always skipped — there is no
    dotted target to match either way.
    """
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if skip_relative and node.level and node.level > 0:
                continue
            if node.module:
                out.append(node.module)
    return out


def _prefix_match(module: str, prefix: str) -> bool:
    """True if ``module`` equals ``prefix`` or starts with ``prefix + "."``.

    The trailing-dot anchor stops ``kairix.providers_helpers`` from matching
    the ``kairix.providers`` prefix.
    """
    return module == prefix or module.startswith(prefix + ".")


def _under_prefix(rel: Path, prefix: str) -> bool:
    """True if repo-relative ``rel`` sits under directory ``prefix``."""
    prefix_parts = Path(prefix).parts
    parts = rel.parts
    return len(parts) >= len(prefix_parts) and tuple(parts[: len(prefix_parts)]) == prefix_parts


# ── mode: prefix ─────────────────────────────────────────────────────────


def _file_has_forbidden_prefix(tree: ast.AST, forbidden: tuple[str, ...]) -> bool:
    """True if any import in ``tree`` targets one of the ``forbidden`` prefixes."""
    for module in _import_modules(tree):
        if any(_prefix_match(module, prefix) for prefix in forbidden):
            return True
    return False


# ── mode: sibling-plugin ─────────────────────────────────────────────────


def _owning_plugin(rel: Path, plugin_root: str) -> str | None:
    """The plugin directory name owning ``rel`` (first segment under
    ``plugin_root``), or ``None`` if ``rel`` sits at the top level of the
    plugin tree (so isn't part of any plugin).
    """
    root_parts = Path(plugin_root).parts
    parts = rel.parts
    if len(parts) < len(root_parts) + 2:
        # No path segment between <plugin_root> and the filename → top-level
        # scaffolding (e.g. _base.py, __init__.py), not inside a plugin.
        return None
    if tuple(parts[: len(root_parts)]) != root_parts:
        return None
    return parts[len(root_parts)]


def _is_cross_plugin(module: str, owning: str, plugin_root: str) -> bool:
    """True if ``module`` reaches a sibling plugin under ``plugin_root`` other
    than ``owning``.

    ``<plugin_root-as-dotted>._base`` and the package itself are shared
    scaffolding — never cross-plugin.
    """
    dotted = ".".join(Path(plugin_root).parts) + "."
    if not module.startswith(dotted):
        return False
    head = module[len(dotted) :].split(".", 1)[0]
    if not head or head.startswith("_"):
        return False
    return head != owning


# ── mode: sync-lib ───────────────────────────────────────────────────────


def _is_sync_lib_import(module: str, sync_roots: tuple[str, ...]) -> bool:
    """True if ``module`` references a change-detection library.

    The first dotted segment must be in ``sync_roots``. ``slack_sdk``
    additionally requires an rtm / socket_mode submodule tail so the one-shot
    Web API surface is not flagged.
    """
    parts = module.split(".")
    if not parts:
        return False
    root = parts[0]
    if root not in sync_roots:
        return False
    if root == _SLACK_ROOT:
        return any(tail in parts[1:] for tail in _SLACK_SYNC_TAILS)
    return True


# ── enumeration + dispatch ───────────────────────────────────────────────


def _iter_python_files(root: Path, rel_roots: tuple[str, ...]) -> list[Path]:
    """Every ``.py`` file under each of ``rel_roots`` (relative to ``root``),
    skipping ``__pycache__``.
    """
    out: list[Path] = []
    for rel_root in rel_roots:
        base = root / rel_root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            out.append(path)
    return out


def _repo_relative(path: Path, root: Path) -> Path | None:
    """Resolve ``path`` to a repo-relative Path under ``root``, or ``None`` if
    it escapes the root."""
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _file_violates(rule: ImportBoundaryRule, path: Path, rel: Path) -> bool:
    """Apply ``rule``'s mode discriminator to the file at ``path`` (repo-relative
    ``rel``). Returns True when the file violates the rule.

    The ``prefix`` (F26/F34/F44) and ``sibling-plugin`` (F27/F35) modes use the
    level-AGNOSTIC ``_import_modules`` (default ``skip_relative=False``) because
    their originals inspected ``ImportFrom.module`` with no ``node.level`` guard.
    The ``sync-lib`` mode (F37) passes ``skip_relative=True`` to match its
    original ``_import_targets``, the one detector that guards ``node.level``."""
    if rule.mode == "prefix":
        if any(_under_prefix(rel, ex) or str(rel) == ex for ex in rule.exempt):
            return False
        tree = _parse(path)
        return tree is not None and _file_has_forbidden_prefix(tree, rule.forbidden_prefixes)

    if rule.mode == "sibling-plugin":
        owning = _owning_plugin(rel, rule.roots[0])
        if owning is None or owning.startswith("_"):
            return False
        tree = _parse(path)
        if tree is None:
            return False
        for module in _import_modules(tree):
            if _is_cross_plugin(module, owning, rule.roots[0]):
                return True
            if any(_prefix_match(module, prefix) for prefix in rule.extra_forbidden_prefixes):
                return True
        return False

    # mode == "sync-lib"
    tree = _parse(path)
    if tree is None:
        return False
    modules = _import_modules(tree, skip_relative=True)
    if not any(_is_sync_lib_import(m, rule.forbidden_prefixes) for m in modules):
        return False
    return not any(_under_prefix(rel, allowed) for allowed in rule.allowed_roots)


def collect_violations_for(rule: ImportBoundaryRule, root: Path = REPO_ROOT) -> set[Path]:
    """Walk ``rule``'s in-scope tree under ``root`` and return the set of
    repo-relative paths that violate the rule.
    """
    violations: set[Path] = set()
    for path in _iter_python_files(root, rule.roots):
        rel = _repo_relative(path, root)
        if rel is None:
            continue
        if _file_violates(rule, path, rel):
            violations.add(rel)
    return violations


# ── the rule table ───────────────────────────────────────────────────────
#
# Remediation text is the one piece of per-rule prose the unit tests read
# back (``detector.REMEDIATION``). It is authored in each shim and injected
# here via :func:`register`, keeping the engine remediation-agnostic while
# the table stays the single lookup surface.

_RULES: dict[str, ImportBoundaryRule] = {}


def register(rule: ImportBoundaryRule) -> ImportBoundaryRule:
    """Register ``rule`` in the engine table keyed by ``rule.name`` and return
    it (so a shim can ``RULE = register(ImportBoundaryRule(...))``)."""
    _RULES[rule.name] = rule
    return rule


def rule_for(name: str) -> ImportBoundaryRule:
    """Return the registered :class:`ImportBoundaryRule` named ``name``."""
    return _RULES[name]
