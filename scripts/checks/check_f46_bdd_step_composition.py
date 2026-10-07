"""F46: BDD step implementations must call factory-composed production code.

Step implementations under ``tests/bdd/steps/*.py`` must, somewhere in
their call graph (depth ≤ 2), invoke one of the sanctioned entry points:

  - A CLI entry point: ``kairix.cli.main`` OR a per-subcommand
    ``main(...)`` function under ``kairix/**/cli.py`` or
    ``kairix/<x>_cli.py``.
  - An MCP tool function: the callable wrapped by a ``@server.tool()``
    decorator in ``kairix/agents/mcp/server.py``.
  - A factory constructor: ``kairix.core.factory.build_*``
    (e.g. ``build_search_pipeline``, future ``build_embed_pipeline``).

Direct construction of ``SearchPipeline(...)``, ``EmbedPipeline(...)``,
``ConnectorPipeline(...)``, ``IngestPipeline(...)`` in a step file is
disallowed unless via the sanctioned entry points above. The detector
flags a step file when:

  1. NONE of its ``@given/@when/@then/@step`` decorated functions reach
     a sanctioned entry point in their depth-≤-2 call graph (function
     body call sites + names called in helper functions defined at
     module top level), AND
  2. the file constructs a ``*Pipeline`` class directly somewhere.

There is no grandfathering: every violation fails the gate.

``main()`` gates the violation set via
:func:`tc_fitness.gate` and returns the exit code. The catalogue-driven
runner dispatches it in-process (#499 Phase 2 stage 4a); the former
``check-f46-bdd-step-composition.sh`` delegator wrapper was retired then.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcp_registry import registered_mcp_tool_names
from tc_fitness import REPO_ROOT, gate, repo_relative

REMEDIATION = """Refactor the step file to route work through a sanctioned
entry point (CLI main / MCP tool function / factory.build_*) instead of
constructing a *Pipeline directly.

fix: use factory.build_search_pipeline(paths=FakePaths(...)) — see
tests/integration/test_vec_index_lifecycle.py for the canonical pattern,
and docs/architecture/test-discipline-hardening.md §4.1.
next: replace direct *Pipeline(...) construction with
factory.build_<pipeline>(paths=FakePaths(...)) and a
registry=FakeProviderRegistry(...) where embed is in scope.
run: python3 scripts/checks/check_f46_bdd_step_composition.py

Pass example:
  from kairix.core import factory
  from tests.fakes import FakePaths, FakeProviderRegistry

  @when("I run a search")
  def run_search() -> None:
      pipe = factory.build_search_pipeline(
          paths=FakePaths(),
          registry=FakeProviderRegistry(),
      )
      _state["result"] = pipe.search("query")

Forbidden example:
  from kairix.core.search.pipeline import SearchPipeline

  @when("I run a search")
  def run_search() -> None:
      pipe = SearchPipeline(...)              # F46 — direct construction
      _state["result"] = pipe.search("query")
"""

# Pipeline classes whose direct construction (in a step file with no
# sanctioned-entry-point call) is the F46 violation.
_PIPELINE_CLASS_NAMES: frozenset[str] = frozenset(
    {
        "SearchPipeline",
        "EmbedPipeline",
        "ConnectorPipeline",
        "IngestPipeline",
    }
)

# Decorator names that mark a pytest-bdd step function.
_STEP_DECORATOR_NAMES: frozenset[str] = frozenset({"given", "when", "then", "step"})


def _discover_mcp_tool_names(repo_root: Path) -> set[str]:
    """Return the set of registered MCP tool names from the catalogue in
    ``kairix/agents/mcp/server.py``.

    A step that calls one of these names by bare identifier (e.g.
    ``search(...)``, ``entity(...)``) counts as routing through the MCP
    tool surface. Post-PLA-318 registration is catalogue-driven, so the set
    is read from the ``_cap(...)`` rows (each row's ``mcp_tool`` /
    ``escalate_via``; see ``_mcp_registry``) rather than from ``@server.tool``
    decorators that no longer exist.
    """
    return registered_mcp_tool_names(repo_root / "kairix" / "agents" / "mcp" / "server.py")


def _call_callee(call: ast.Call) -> tuple[str | None, bool]:
    """Best-effort extraction of the callable's name from a Call node.

    Returns ``(name, is_bare_name)``:
      - ``name`` is the rightmost attribute or Name.id, or None for
        dynamic / subscripted callees.
      - ``is_bare_name`` is True only when the callee is an unqualified
        ``ast.Name`` (i.e. ``search(...)`` not ``pipe.search(...)``).
        Used to disambiguate MCP-tool-name matches from arbitrary
        ``.search(...)`` method calls on local objects.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id, True
    if isinstance(func, ast.Attribute):
        return func.attr, False
    return None, False


def _is_sanctioned_callee(
    name: str | None,
    is_bare_name: bool,
    mcp_tool_names_in_scope: frozenset[str],
) -> bool:
    """True if ``name`` matches a sanctioned entry-point identifier.

    Matches:
      - ``main`` (CLI entry — both ``kairix.cli.main`` and per-subcommand
        ``main`` from any ``cli.py`` / ``*_cli.py`` resolve here on the
        attribute side; either form counts).
      - Any ``build_*`` prefix (factory family — ``build_search_pipeline``,
        future ``build_embed_pipeline``, etc).
      - Any registered MCP tool name, BUT only when called as a bare
        ``Name`` and the name is in scope via an import from
        ``kairix.agents.mcp.server``. This guards against false
        positives from common method calls like ``pipe.search(query)``.
    """
    if name is None:
        return False
    if name == "main":
        return True
    if name.startswith("build_"):
        return True
    if is_bare_name and name in mcp_tool_names_in_scope:
        return True
    return False


def _collect_module_functions(tree: ast.AST) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Map every top-level function name in ``tree`` to its def node.

    Used so we can trace depth-2 calls: a step body call to a helper
    function defined in the same module gets expanded into the helper's
    own call set.
    """
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    if not isinstance(tree, ast.Module):
        return out
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[stmt.name] = stmt
    return out


def _calls_in_function(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[str, bool]]:
    """Return the list of ``(name, is_bare_name)`` tuples for every call
    site inside ``fn``'s body (any depth within the function — but NOT
    following function references; that's the caller's job via depth-2
    expansion).

    Returns tuples instead of names so the MCP-tool-name disambiguation
    in ``_is_sanctioned_callee`` (bare ``Name`` only) survives the round
    trip.
    """
    out: list[tuple[str, bool]] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            name, is_bare = _call_callee(node)
            if name is not None:
                out.append((name, is_bare))
    return out


def _mcp_tool_imports_in_scope(
    tree: ast.AST,
    mcp_tool_names: frozenset[str],
) -> frozenset[str]:
    """Return the subset of ``mcp_tool_names`` that the module imports
    from ``kairix.agents.mcp.server`` (or an alias of that module).

    A bare ``Name`` call to one of these names counts as routing through
    the MCP tool surface. Imports from other paths (e.g.
    ``from kairix.core.search import search``) do NOT count.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module != "kairix.agents.mcp.server":
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if local in mcp_tool_names:
                    out.add(local)
    return frozenset(out)


def _is_step_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if ``node`` carries a pytest-bdd step decorator."""
    for deco in node.decorator_list:
        # @given(...), @when(...), @then(...), @step(...)
        if isinstance(deco, ast.Call):
            fn = deco.func
            if isinstance(fn, ast.Name) and fn.id in _STEP_DECORATOR_NAMES:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr in _STEP_DECORATOR_NAMES:
                return True
        # @given / @when / @then / @step (no parens)
        if isinstance(deco, ast.Name) and deco.id in _STEP_DECORATOR_NAMES:
            return True
        if isinstance(deco, ast.Attribute) and deco.attr in _STEP_DECORATOR_NAMES:
            return True
    return False


def _step_reaches_sanctioned(
    step_fn: ast.FunctionDef | ast.AsyncFunctionDef,
    module_fns: dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
    mcp_tool_names_in_scope: frozenset[str],
) -> bool:
    """True if the depth-≤-2 call graph rooted at ``step_fn`` reaches a
    sanctioned entry-point name.

    Depth 1: names directly called in ``step_fn``.
    Depth 2: for each name that resolves to a top-level helper defined
    in the same module, expand to the helper's own called names.
    """
    direct = _calls_in_function(step_fn)
    for name, is_bare in direct:
        if _is_sanctioned_callee(name, is_bare, mcp_tool_names_in_scope):
            return True
    # Depth 2 — expand any direct call that targets a sibling top-level
    # function defined in the same module.
    for name, _is_bare in direct:
        helper = module_fns.get(name)
        if helper is None:
            continue
        for inner_name, inner_is_bare in _calls_in_function(helper):
            if _is_sanctioned_callee(inner_name, inner_is_bare, mcp_tool_names_in_scope):
                return True
    return False


def _constructs_pipeline_directly(tree: ast.AST) -> bool:
    """True if ``tree`` calls a ``*Pipeline`` class constructor directly
    anywhere — ``SearchPipeline(...)``, ``EmbedPipeline(...)``,
    ``ConnectorPipeline(...)``, ``IngestPipeline(...)``.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name, _is_bare = _call_callee(node)
        if name in _PIPELINE_CLASS_NAMES:
            return True
    return False


def file_has_violation(
    path: Path,
    mcp_tool_names: frozenset[str],
) -> bool:
    """True if ``path`` is a step file that:

    - constructs a ``*Pipeline`` class directly, AND
    - has no step whose depth-≤-2 call graph reaches a sanctioned
      entry-point name.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False

    if not _constructs_pipeline_directly(tree):
        return False

    module_fns = _collect_module_functions(tree)
    step_fns = [fn for fn in module_fns.values() if _is_step_function(fn)]
    if not step_fns:
        # No step decorators at all — a helper module, not a step file
        # in the F46-relevant sense. Don't flag.
        return False
    in_scope = _mcp_tool_imports_in_scope(tree, mcp_tool_names)
    for step_fn in step_fns:
        if _step_reaches_sanctioned(step_fn, module_fns, in_scope):
            return False
    return True


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every ``tests/bdd/steps/*.py`` file and return the set of
    repo-relative paths that violate F46.
    """
    steps_dir = repo_root / "tests" / "bdd" / "steps"
    if not steps_dir.exists():
        return set()
    mcp_tool_names = frozenset(_discover_mcp_tool_names(repo_root))
    violations: set[Path] = set()
    for path in sorted(steps_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        if file_has_violation(path, mcp_tool_names):
            try:
                violations.add(path.resolve().relative_to(repo_root))
            except ValueError:
                violations.add(repo_relative(path))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f46", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
