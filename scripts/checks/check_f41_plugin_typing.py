"""F41: every plugin tree carries a ``py.typed`` marker AND has no
unjustified ``# type: ignore`` directives.

Plugins under ``kairix/connectors/<name>/``, ``kairix/extractors/<name>/``,
and ``kairix/providers/<name>/`` are independently shippable units
(PEP 561). They MUST advertise typed-ness to downstream consumers (the
``py.typed`` marker file in the package root) and they MUST stay
mypy-strict-clean — the canonical reference being whatever
``mypy --strict`` reports.

The fitness function splits the strictness contract across two
enforcement points:

  1. **Static (this check, pre-commit + Stage 0):** verify that the
     plugin root carries ``py.typed`` AND that every ``# type: ignore``
     directive in the plugin's ``.py`` files has an F3-style rationale
     (a ``—`` or `` - `` separator followed by free-text after the
     directive on the same line). Cheap; runs in milliseconds.

  2. **Full mypy-strict-clean (delegated):** the repo-wide
     ``mypy --strict kairix`` invocation already runs inside
     ``safe-commit.sh`` and CI Stage 2. That gate transitively
     covers every plugin file. Re-running ``mypy --strict <plugin>``
     per-plugin from this check would duplicate that pass at
     non-trivial cost (mypy cold-start + cache hydration), so the
     contract is: this static check + the existing whole-tree mypy
     run together satisfy F41. The static check is what blocks at
     commit-time; mypy is what catches the inference-dependent
     violations the static layer cannot see.

Plugin discovery: every immediate subdirectory of
``kairix/{connectors,extractors,providers}/`` whose name is not
``_``-prefixed and is not in the small allow-list (``__pycache__``)
is a plugin. Files at the trees' roots (``__init__.py``, ``_base.py``)
are NOT plugins.

Violations are reported by the plugin directory path (one entry per
plugin missing ``py.typed`` or carrying an unjustified ``type: ignore``).
There is no grandfathering: every violation fails the gate.

If none of ``kairix/connectors/``, ``kairix/extractors/``,
``kairix/providers/`` exist (fresh checkout), or all are empty of
plugin subdirectories, the check passes trivially.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import REPO_ROOT

_PLUGIN_TREES_REL: tuple[Path, ...] = (
    Path("kairix") / "connectors",
    Path("kairix") / "extractors",
    Path("kairix") / "providers",
)

# Names under a plugin tree that are NOT plugins (shared scaffolding
# or cache directories).
_NON_PLUGIN_NAMES: frozenset[str] = frozenset({"__pycache__"})

# A ``# type: ignore`` with a rationale: anything that follows the
# directive (or its ``[code]`` qualifier) on the same line counts —
# the F3 rationale rule treats free-text after the suppression as the
# justification. A bare ``# type: ignore`` (or ``# type: ignore[code]``)
# with nothing else on the line is unjustified.
_TYPE_IGNORE_RE = re.compile(r"#\s*type:\s*ignore(?:\[[^\]]*\])?(?P<trailing>.*)$")

REMEDIATION = """F41: a plugin is missing ``py.typed`` OR carries a
bare ``# type: ignore`` without an F3 rationale.

Plugins under ``kairix/{connectors,extractors,providers}/<name>/`` are
shipped as PEP-561 typed packages. Without ``py.typed`` downstream
mypy runs treat the plugin as untyped; without rationale on a
``type: ignore`` we lose the audit trail when the suppression
outlives the bug it was hiding.

fix: (a) create an empty ``kairix/<tree>/<name>/py.typed`` file (zero
bytes is fine — it's a marker). (b) for every bare
``# type: ignore`` in the plugin, append a `` — <reason>`` (or
`` - <reason>``) explaining what's being suppressed AND why mypy
can't see through to it. If the ignore is no longer needed, delete
it.
next: re-run python3 scripts/checks/check_f41_plugin_typing.py to
confirm the gate goes green, then run the whole-tree
mypy --strict kairix (already wired into safe-commit.sh) to catch
the inference-dependent violations the static check cannot see.
run: bash scripts/safe-commit.sh \"chore(<plugin>): add py.typed + rationalise type-ignore\"

Pass example:
  # kairix/providers/openai/py.typed   (zero-byte marker)
  # kairix/providers/openai/provider.py
  client = make_openai_client(...)  # type: ignore[arg-type] — openai SDK v1.x still uses Any in this kwarg

Forbidden example: (bare directive, plus the plugin has no py.typed)
  # kairix/providers/openai/provider.py
  # client = make_openai_client(...)  ## (bare) type-ignore[arg-type]
  # (and no py.typed marker exists in the plugin root)

Why: PEP 561 requires the marker for downstream tools to honour the
package's type hints. F3 requires rationale on every per-line
suppression. F41 is the plugin-tree-specific reification of both —
the plugin boundary is where typed-ness matters most because the
plugin can be loaded into an arbitrary host process via entry
points."""


def _discover_plugins(tree_root: Path) -> list[Path]:
    """List plugin directories under ``tree_root``.

    Skips ``_``-prefixed names (shared scaffolding) and the cache
    allow-list. Files at the tree root are never plugins. Returns
    absolute paths sorted by name; empty list if the root doesn't
    exist.
    """
    if not tree_root.exists():
        return []
    out: list[Path] = []
    for child in sorted(tree_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_"):
            continue
        if child.name in _NON_PLUGIN_NAMES:
            continue
        out.append(child)
    return out


def _type_ignore_unjustified(line: str) -> bool:
    """True if ``line`` carries a ``# type: ignore`` whose trailing text
    is blank (no rationale).

    A rationale is any non-whitespace content after the directive (or
    its ``[code]`` qualifier) on the same line. The F3 convention is
    `` — <reason>`` or `` - <reason>``, but this check accepts ANY
    trailing content — the human-grade rationale is enforced by F3's
    own audit; F41 only refuses the bare directive.
    """
    match = _TYPE_IGNORE_RE.search(line)
    if not match:
        return False
    trailing = match.group("trailing").strip()
    return trailing == ""


def _plugin_has_unjustified_ignore(plugin_dir: Path) -> bool:
    """True if any ``.py`` file under ``plugin_dir`` has a bare
    ``# type: ignore`` (no rationale on the same line).
    """
    for py_file in plugin_dir.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        try:
            text = py_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            if _type_ignore_unjustified(line):
                return True
    return False


def _plugin_violates(plugin_dir: Path) -> bool:
    """True if the plugin lacks ``py.typed`` OR has any unjustified
    ``# type: ignore``.
    """
    if not (plugin_dir / "py.typed").is_file():
        return True
    return _plugin_has_unjustified_ignore(plugin_dir)


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every plugin tree under ``<repo_root>/kairix/`` and return
    the repo-relative plugin directory paths that violate F41.

    Returns an empty set if no plugin trees exist or hold any plugins.
    """
    violations: set[Path] = set()
    for tree_rel in _PLUGIN_TREES_REL:
        tree_root = repo_root / tree_rel
        for plugin_dir in _discover_plugins(tree_root):
            if _plugin_violates(plugin_dir):
                try:
                    violations.add(plugin_dir.resolve().relative_to(repo_root))
                except ValueError:
                    violations.add(tree_rel / plugin_dir.name)
    return violations


class F41(FitnessRule):
    """F41 as a FitnessRule subclass — see module docstring.

    Overrides :meth:`enumerate_files` to yield plugin DIRECTORIES (not
    files) because the violation unit for F41 is a plugin tree, not an
    individual ``.py`` file. :meth:`is_in_scope` returns True
    unconditionally since the directory enumeration is already
    restricted to plugin trees.
    """

    name = "f41"
    remediation = REMEDIATION
    roots = tuple(str(p) for p in _PLUGIN_TREES_REL)

    def enumerate_files(self) -> list[Path]:
        out: list[Path] = []
        for tree_rel in _PLUGIN_TREES_REL:
            tree_root = self._repo_root / tree_rel
            for plugin_dir in _discover_plugins(tree_root):
                out.append(plugin_dir)
        return out

    def is_in_scope(self, rel: str) -> bool:
        return True

    def file_has_violation(self, path: Path) -> bool:
        return _plugin_violates(path)


def main() -> int:
    return F41().run()


if __name__ == "__main__":
    sys.exit(main())
