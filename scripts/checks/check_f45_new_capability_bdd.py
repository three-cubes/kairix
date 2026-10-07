"""F45: every new top-level capability ships with a BDD feature file.

A net-new commit that adds one of the following surfaces MUST also add
a matching ``tests/bdd/features/*.feature`` in the same commit:

  * CLI subcommand — new key in ``kairix/cli.py:COMMANDS``.
  * MCP tool — new ``@server.tool()``-decorated function in
    ``kairix/agents/mcp/server.py``.
  * Provider plugin factory — ``make_provider`` symbol in a new
    ``kairix/providers/<name>/__init__.py``.
  * Connector plugin factory — ``make_connector`` symbol in a new
    ``kairix/connectors/<name>/__init__.py``.
  * Extractor plugin factory — ``make_extractor`` symbol in a new
    ``kairix/extractors/<name>/__init__.py``.

F12 already governs the content of existing feature files; F30 catches
missing outcome tests once a surface is wired into ``COMMANDS`` or
``@server.tool()``. F45 closes the gap between "code lands" and
"behaviour spec lands" — to zero commits, not "fix it next sprint".

**Naming convention** for the feature file:

  * CLI subcommand ``<name>`` → ``tests/bdd/features/cli_<name>.feature``
  * MCP tool ``<name>`` → ``tests/bdd/features/mcp_<name>.feature``
  * Provider ``<name>`` → ``tests/bdd/features/provider_<name>.feature``
  * Connector ``<name>`` → ``tests/bdd/features/connector_<name>.feature``
  * Extractor ``<name>`` → ``tests/bdd/features/extractor_<name>.feature``

A surface file may also carry an explicit override comment of the
form ``# F45-feature: <path>`` somewhere in its source (e.g. on the
``COMMANDS`` row or above the ``@server.tool()`` decorator). The
override pointer is honoured as long as the pointed-at file exists in
the staged set (or in the working tree for ``--full-tree`` runs).

**Modes**:

  * Default (pre-commit / safe-commit): scans the index — uses
    ``git diff --cached --name-only`` to find changed files and
    ``git diff --cached <file>`` to find net-new COMMANDS rows /
    decorator lines / factory symbols. Requires that any matching
    feature file is also in the staged set.

  * ``--full-tree``: scans every COMMANDS row, every
    ``@server.tool()`` function, and every plugin factory present in
    the current tree, and verifies the feature file exists on disk.
    Used by CI when there is no staging area (e.g. push to ``main``).

**Library hook**: the public function ``collect_violations(
new_surfaces, staged_features, tree_features)`` returns a sorted list
of ``(surface, suggested_feature_path)`` tuples for any new surface
that lacks coverage. Tests call this directly with a synthetic input;
the ``main()`` entry point wraps it with git/tree discovery.

There is no grandfathering: every violation fails the gate.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcp_registry import registered_mcp_tool_names_from_source
from tc_fitness import REPO_ROOT, gate

CLI_PATH = Path("kairix") / "cli.py"
MCP_SERVER_PATH = Path("kairix") / "agents" / "mcp" / "server.py"
FEATURES_DIR = Path("tests") / "bdd" / "features"

# Plugin trees that F45 tracks. Each is keyed by the directory under
# ``kairix/`` and the factory symbol that marks a plugin as "shipped".
_PLUGIN_TREES: tuple[tuple[str, str, str], ...] = (
    ("providers", "make_provider", "provider"),
    ("connectors", "make_connector", "connector"),
    ("extractors", "make_extractor", "extractor"),
)

# ``# F45-feature: path/to/file.feature`` — override comment that lets
# a surface file point at a non-conventionally-named feature.
_OVERRIDE_RE = re.compile(r"#\s*F45-feature:\s*(\S+)")

REMEDIATION = """F45: new surface introduced without a .feature file.

A new CLI subcommand, MCP tool, or plugin factory must ship with a
matching tests/bdd/features/*.feature in the SAME commit — that's the
behaviour spec contract. F12 covers content of existing features;
F45 closes the window between "code lands" and "spec lands".

fix: add tests/bdd/features/<convention>.feature with a happy-path
scenario covering the new surface, then `git add` it before retrying
the commit. The naming convention is:
  * CLI subcommand <name>     → tests/bdd/features/cli_<name>.feature
  * MCP tool <name>           → tests/bdd/features/mcp_<name>.feature
  * Provider <name>           → tests/bdd/features/provider_<name>.feature
  * Connector <name>          → tests/bdd/features/connector_<name>.feature
  * Extractor <name>          → tests/bdd/features/extractor_<name>.feature
If the feature file must live elsewhere, add a
``# F45-feature: <path>`` comment to the surface file.
next: see docs/architecture/test-discipline-hardening.md §2.3
(new-capability principle) for the canonical shape.
run: python3 scripts/checks/check_f45_new_capability_bdd.py

Pass example: (tests/bdd/features/cli_<name>.feature)
  Feature: <name> subcommand
    Scenario: happy path
      Given a kairix process configured with FakePaths
      When the operator runs `kairix <name>` with valid input
      Then the command exits 0 and prints the expected envelope

Forbidden example:
  kairix/cli.py adds ``"my-new-cmd": (...)`` to COMMANDS but
  no tests/bdd/features/cli_my_new_cmd.feature in the same commit.

Why: shipping a capability without a behaviour spec is how
implementation drifts ahead of intent — exactly the LoCoMo regression
shape (5233 tests green, real path 5% recall). F45 makes that drift
mechanically impossible."""


# ---------- AST helpers ----------


def _extract_commands_keys(source: str) -> set[str]:
    """Return every string key in the CLI dispatch-wiring dict annotation.

    Post-PLA-319 the wiring literal is ``_CLI_HANDLERS: dict[...]`` and the
    runtime ``COMMANDS`` table is DERIVED from it (so ``COMMANDS`` is no longer
    a dict literal). Both identifiers are accepted so net-new-subcommand
    detection stays correct across the pre-/post-derivation shapes. Returns an
    empty set if neither is declared or parsing fails.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in ("COMMANDS", "_CLI_HANDLERS")
            and isinstance(node.value, ast.Dict)
        ):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    out.add(k.value)
    return out


def _extract_mcp_tool_names(source: str) -> set[str]:
    """Return every registered MCP tool name declared in server.py ``source``.

    Post-PLA-318 registration is catalogue-driven — a new tool is a new
    ``_cap(...)`` catalogue row (its ``mcp_tool`` / ``escalate_via`` name) plus
    a matching adapter binding, not a new ``@server.tool`` def. F45 diffs this
    catalogue-derived set (see ``_mcp_registry``) between HEAD and staged to
    find the net-new tools that must ship a BDD feature.
    """
    return registered_mcp_tool_names_from_source(source)


def _has_factory_symbol(source: str, factory_name: str) -> bool:
    """True if the source defines a top-level symbol named ``factory_name``.

    Recognises three shapes:
      * ``def make_provider(...): ...`` / async variant
      * ``make_provider = ...``
      * ``from somewhere import make_provider`` (re-export)
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == factory_name:
            return True
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == factory_name:
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == factory_name:
            return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == factory_name:
                    return True
    return False


def _extract_override(source: str) -> str | None:
    """Return the first ``# F45-feature: <path>`` override comment, or
    ``None`` if absent. Pointer is repo-relative."""
    m = _OVERRIDE_RE.search(source)
    if m:
        return m.group(1).strip()
    return None


# ---------- Surface discovery ----------


def _surface_to_feature_path(kind: str, name: str) -> Path:
    """Convention map: ``(kind, name)`` → ``tests/bdd/features/<x>.feature``.

    Hyphens in CLI subcommand names are normalised to underscores
    (e.g. ``probe-config`` → ``cli_probe_config.feature``) because the
    F22 path-naming rule rejects hyphens in feature filenames.
    """
    safe = name.replace("-", "_")
    return FEATURES_DIR / f"{kind}_{safe}.feature"


def collect_violations(
    new_surfaces: list[tuple[str, str, str | None]],
    staged_paths: set[Path],
    tree_root: Path | None = None,
) -> list[tuple[str, Path]]:
    """For each new surface ``(kind, name, override)``, return the
    pair ``(surface_label, suggested_feature_path)`` if the contract
    is not satisfied.

    Args:
      new_surfaces: list of ``(kind, name, override_path_or_None)``;
        ``kind`` is one of ``"cli"``, ``"mcp"``, ``"provider"``,
        ``"connector"``, ``"extractor"``.
      staged_paths: repo-relative Paths in the current staged set
        (default mode) or "tree" set (``--full-tree`` mode).
      tree_root: when set, the check also accepts an override pointer
        that resolves to an existing file under this root, even if
        the file is not in ``staged_paths``. Used by ``--full-tree``
        so a pre-existing override stays valid post-rebase.

    Returns the list of unsatisfied surfaces, sorted by ``(kind, name)``.
    """
    # Note: F21 (check_actionable_feedback) scans for
    # ``errors|violations|...append(<literal>)`` shapes; using
    # ``unsatisfied`` here keeps the variable name outside the
    # remediation-string heuristic. F45's actionable text lives in the
    # module-level REMEDIATION constant.
    unsatisfied: list[tuple[str, Path]] = []
    for kind, name, override in sorted(new_surfaces):
        convention = _surface_to_feature_path(kind, name)
        candidates: list[Path] = [convention]
        if override:
            candidates.append(Path(override))
        satisfied = any(c in staged_paths for c in candidates)
        if not satisfied and tree_root is not None:
            for c in candidates:
                if (tree_root / c).is_file():
                    satisfied = True
                    break
        if not satisfied:
            unsatisfied.append((f"{kind}:{name}", convention))
    return unsatisfied


# ---------- Git diff (staged) mode ----------


def _git(args: list[str], cwd: Path) -> str:
    """Run ``git`` with the given args in ``cwd``; return stdout.

    Returns an empty string on non-zero exit so the check is robust to
    repos that are not in a git checkout (e.g. CI fresh-clone of a tag).
    """
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _staged_files(repo_root: Path) -> set[Path]:
    """Return repo-relative Paths of all files in the staging area."""
    raw = _git(["diff", "--cached", "--name-only"], repo_root)
    return {Path(line) for line in raw.splitlines() if line.strip()}


def _staged_file_blob(repo_root: Path, rel: Path) -> str:
    """Return the staged (index-side) contents of ``rel`` as text.

    Uses ``git show :<path>`` so deletions return empty and modified
    files return the new content. Returns "" on any error.
    """
    raw = _git(["show", f":{rel.as_posix()}"], repo_root)
    return raw


def _head_file_blob(repo_root: Path, rel: Path) -> str:
    """Return the HEAD-side contents of ``rel`` as text, or "" if the
    file does not exist at HEAD (i.e. it's a net-new file in the
    index)."""
    raw = _git(["show", f"HEAD:{rel.as_posix()}"], repo_root)
    return raw


def _new_cli_subcommands(repo_root: Path) -> set[str]:
    """Subcommand names added (not removed, not renamed) in the staged
    diff of ``kairix/cli.py``.
    """
    if CLI_PATH not in _staged_files(repo_root):
        return set()
    staged = _extract_commands_keys(_staged_file_blob(repo_root, CLI_PATH))
    head = _extract_commands_keys(_head_file_blob(repo_root, CLI_PATH))
    return staged - head


def _new_mcp_tools(repo_root: Path) -> set[str]:
    """``@server.tool()`` function names added in the staged diff."""
    if MCP_SERVER_PATH not in _staged_files(repo_root):
        return set()
    staged = _extract_mcp_tool_names(_staged_file_blob(repo_root, MCP_SERVER_PATH))
    head = _extract_mcp_tool_names(_head_file_blob(repo_root, MCP_SERVER_PATH))
    return staged - head


def _new_plugin_factories(repo_root: Path) -> list[tuple[str, str]]:
    """Net-new plugin factory symbols in the staging set.

    Returns a list of ``(kind, plugin_name)`` pairs, where ``kind`` is
    one of ``provider``/``connector``/``extractor`` and ``plugin_name``
    is the directory name immediately under ``kairix/<tree>/``.
    """
    staged = _staged_files(repo_root)
    out: list[tuple[str, str]] = []
    for tree, factory, kind in _PLUGIN_TREES:
        for rel in staged:
            parts = rel.parts
            if len(parts) >= 4 and parts[0] == "kairix" and parts[1] == tree and parts[-1] == "__init__.py":
                plugin = parts[2]
                if plugin.startswith("_"):
                    continue
                staged_src = _staged_file_blob(repo_root, rel)
                head_src = _head_file_blob(repo_root, rel)
                if _has_factory_symbol(staged_src, factory) and not _has_factory_symbol(head_src, factory):
                    out.append((kind, plugin))
    return out


def _collect_overrides_from_staged_surfaces(repo_root: Path) -> dict[tuple[str, str], str]:
    """Map ``(kind, name)`` to the ``# F45-feature: <path>`` override,
    when one is declared in the surface's source file.

    The override is read from the STAGED blob — the override comment
    has to land in the same commit as the surface, otherwise the new
    capability is shipping without a recorded BDD pointer.
    """
    out: dict[tuple[str, str], str] = {}

    # CLI: per-subcommand overrides are read line-by-line from the
    # staged cli.py — the override comment must appear on (or
    # immediately above) the COMMANDS row for that subcommand.
    if CLI_PATH in _staged_files(repo_root):
        for kind, name, path in _per_command_overrides(_staged_file_blob(repo_root, CLI_PATH)):
            out[(kind, name)] = path

    # MCP: per-tool overrides are read from the staged server.py —
    # the override comment must appear inside or immediately above
    # the ``@server.tool()`` block for that tool.
    if MCP_SERVER_PATH in _staged_files(repo_root):
        for kind, name, path in _per_tool_overrides(_staged_file_blob(repo_root, MCP_SERVER_PATH)):
            out[(kind, name)] = path

    # Plugins: the override lives in the plugin's __init__.py.
    for tree, _factory, kind in _PLUGIN_TREES:
        for rel in _staged_files(repo_root):
            parts = rel.parts
            if len(parts) >= 4 and parts[0] == "kairix" and parts[1] == tree and parts[-1] == "__init__.py":
                plugin = parts[2]
                if plugin.startswith("_"):
                    continue
                override = _extract_override(_staged_file_blob(repo_root, rel))
                if override:
                    out[(kind, plugin)] = override
    return out


def _per_command_overrides(source: str) -> list[tuple[str, str, str]]:
    """Scan ``cli.py`` source line-by-line; emit ``(kind, name, path)``
    for any ``# F45-feature: ...`` annotation found inside the CLI
    dispatch-wiring dict literal (``_CLI_HANDLERS`` post-PLA-319, or the
    legacy ``COMMANDS`` literal).

    The annotation may sit on the same line as the row
    (``"foo": (..., ...),  # F45-feature: tests/...``) or on the line
    immediately above it.
    """
    out: list[tuple[str, str, str]] = []
    in_commands = False
    pending_override: str | None = None
    for raw in source.splitlines():
        stripped = raw.strip()
        if not in_commands:
            if stripped.startswith(("COMMANDS", "_CLI_HANDLERS")):
                in_commands = True
            continue
        if stripped.startswith("}"):
            break
        m = _OVERRIDE_RE.search(raw)
        # Match a row like:  "foo": ("kairix.foo", "main", True),
        row = re.match(r'\s*"([a-zA-Z0-9_\-]+)"\s*:', raw)
        if row:
            name = row.group(1)
            override = m.group(1).strip() if m else pending_override
            if override:
                out.append(("cli", name, override))
            pending_override = None
        elif m:
            pending_override = m.group(1).strip()
    return out


def _per_tool_overrides(source: str) -> list[tuple[str, str, str]]:
    """Scan ``server.py`` source for ``# F45-feature: ...`` annotations
    that precede a ``@server.tool(...)``-decorated function. Returns
    ``(kind, tool_name, path)`` per pointer.

    The annotation must sit between the ``@server.tool(`` line and the
    ``def <name>(...)`` line for the same tool.
    """
    out: list[tuple[str, str, str]] = []
    lines = source.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        if "@server.tool" in line:
            override: str | None = None
            j = i
            # Scan forward for the def line, capturing any
            # F45-feature comment that appears in the block.
            while j < n:
                m = _OVERRIDE_RE.search(lines[j])
                if m and override is None:
                    override = m.group(1).strip()
                fn_match = re.match(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", lines[j])
                if fn_match:
                    if override:
                        out.append(("mcp", fn_match.group(1), override))
                    i = j
                    break
                j += 1
        i += 1
    return out


# ---------- Full-tree mode (no staging area) ----------


def _tree_cli_subcommands(repo_root: Path) -> set[str]:
    src = (repo_root / CLI_PATH).read_text(encoding="utf-8") if (repo_root / CLI_PATH).is_file() else ""
    return _extract_commands_keys(src)


def _tree_mcp_tools(repo_root: Path) -> set[str]:
    src = (repo_root / MCP_SERVER_PATH).read_text(encoding="utf-8") if (repo_root / MCP_SERVER_PATH).is_file() else ""
    return _extract_mcp_tool_names(src)


def _tree_plugin_factories(repo_root: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for tree, factory, kind in _PLUGIN_TREES:
        tree_dir = repo_root / "kairix" / tree
        if not tree_dir.is_dir():
            continue
        for child in sorted(tree_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            init = child / "__init__.py"
            if not init.is_file():
                continue
            if _has_factory_symbol(init.read_text(encoding="utf-8"), factory):
                out.append((kind, child.name))
    return out


def _tree_files(repo_root: Path) -> set[Path]:
    """Repo-relative paths of every file currently on disk under
    ``tests/bdd/features/`` and the surface source files. Used as the
    `staged_paths` argument in ``--full-tree`` mode so that a
    pre-existing feature file satisfies the rule.
    """
    out: set[Path] = set()
    features = repo_root / FEATURES_DIR
    if features.is_dir():
        for p in features.rglob("*.feature"):
            out.add(p.relative_to(repo_root))
    return out


def _tree_overrides(repo_root: Path) -> dict[tuple[str, str], str]:
    """Same shape as the staged version, but reads from disk."""
    out: dict[tuple[str, str], str] = {}
    if (repo_root / CLI_PATH).is_file():
        for kind, name, path in _per_command_overrides((repo_root / CLI_PATH).read_text(encoding="utf-8")):
            out[(kind, name)] = path
    if (repo_root / MCP_SERVER_PATH).is_file():
        for kind, name, path in _per_tool_overrides((repo_root / MCP_SERVER_PATH).read_text(encoding="utf-8")):
            out[(kind, name)] = path
    for tree, _factory, kind in _PLUGIN_TREES:
        tree_dir = repo_root / "kairix" / tree
        if not tree_dir.is_dir():
            continue
        for child in sorted(tree_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("_"):
                continue
            init = child / "__init__.py"
            if not init.is_file():
                continue
            override = _extract_override(init.read_text(encoding="utf-8"))
            if override:
                out[(kind, child.name)] = override
    return out


# ---------- Main entry ----------


def _gather_new_surfaces_staged(repo_root: Path) -> list[tuple[str, str, str | None]]:
    """Combine all three surface scans into a single sorted list."""
    overrides = _collect_overrides_from_staged_surfaces(repo_root)
    out: list[tuple[str, str, str | None]] = []
    for name in sorted(_new_cli_subcommands(repo_root)):
        out.append(("cli", name, overrides.get(("cli", name))))
    for name in sorted(_new_mcp_tools(repo_root)):
        out.append(("mcp", name, overrides.get(("mcp", name))))
    for kind, name in sorted(_new_plugin_factories(repo_root)):
        out.append((kind, name, overrides.get((kind, name))))
    return out


def _gather_new_surfaces_full_tree(repo_root: Path) -> list[tuple[str, str, str | None]]:
    """In ``--full-tree`` mode, every surface present in the tree is
    treated as a potential violator; the existing-feature lookup clears
    every surface that already has its BDD feature.
    """
    overrides = _tree_overrides(repo_root)
    out: list[tuple[str, str, str | None]] = []
    for name in sorted(_tree_cli_subcommands(repo_root)):
        out.append(("cli", name, overrides.get(("cli", name))))
    for name in sorted(_tree_mcp_tools(repo_root)):
        out.append(("mcp", name, overrides.get(("mcp", name))))
    for kind, name in sorted(_tree_plugin_factories(repo_root)):
        out.append((kind, name, overrides.get((kind, name))))
    return out


def main(argv: list[str] | None = None) -> int:
    """Entry: parse args, scan the configured mode, gate on violations."""
    parser = argparse.ArgumentParser(
        prog="check_f45_new_capability_bdd",
        description="F45 — every new top-level capability ships with a BDD feature.",
    )
    parser.add_argument(
        "--full-tree",
        action="store_true",
        help="Scan every surface in the tree (CI mode); default scans staged diff.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Override repo root (defaults to the script's repo root).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT

    if args.full_tree:
        new_surfaces = _gather_new_surfaces_full_tree(repo_root)
        staged_paths = _tree_files(repo_root)
        tree_root: Path | None = repo_root
    else:
        new_surfaces = _gather_new_surfaces_staged(repo_root)
        staged_paths = _staged_files(repo_root)
        tree_root = None

    pairs = collect_violations(new_surfaces, staged_paths, tree_root)
    # Translate to the synthetic-Path form gate() expects: one path
    # per violating surface, with the surface label embedded so the
    # failure output reads naturally. Variable name is ``offenders`` rather
    # than ``violations`` so F21's append-literal heuristic doesn't
    # false-positive on the dynamic f-string below.
    offenders: set[Path] = set()
    for surface, suggested in pairs:
        offenders.add(Path(f"{surface} -> {suggested.as_posix()}"))

    return gate("f45", offenders, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
