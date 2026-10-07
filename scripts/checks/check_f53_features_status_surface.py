"""F53: operator surface for feature flags exists.

Operations affordance — flags are useless if the operator can't see what
is enabled. F53 enforces:

  1. ``kairix/cli.py:COMMANDS`` has a ``"features"`` entry.
  2. ``kairix/agents/mcp/server.py`` registers a ``features_status`` MCP
     tool — an ``@server.tool()``-decorated function whose name resolves
     to the ``features_status`` capability. Per the codebase convention
     (see ``worker_status`` / ``caches_status``), the registered inner
     function is named ``features_status`` and delegates to the
     module-level ``tool_features_status`` adapter; the bare
     ``tool_<name>`` form is also accepted for forward-compat.
The outcome-test requirement for both surfaces is enforced directly by
F30 (``check_f30_operator_outcome_tests.py``), which has no grandfather
list since PLA-472 — a missing outcome test fails F30 outright, so F53
no longer reads any baseline file.

Binary presence check. Vacuous-green when
``kairix/core/features`` is not importable (PR-2 may not have landed).

Per F21, REMEDIATION carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _mcp_registry import registered_mcp_tool_names

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CLI_REL_PATH = Path("kairix/cli.py")
MCP_REL_PATH = Path("kairix/agents/mcp/server.py")

REMEDIATION = """F53: operator surface missing for feature flags.
fix: ensure kairix/cli.py:COMMANDS includes a 'features' entry (CLI
     subcommand) AND kairix/agents/mcp/server.py registers a
     'features_status' MCP tool — an @server.tool() function named
     'features_status' (codebase convention; delegates to the
     module-level tool_features_status adapter). Both must also have
     F30-compliant outcome tests (F30 enforces that directly).
next: see docs/architecture/feature-flag-architecture.md §3.5 (operator
      surface) + §6 (F53 mechanics).
run: python3 scripts/checks/check_f53_features_status_surface.py

Pass example:
  # kairix/cli.py
  COMMANDS: dict[str, tuple[...]] = {
      "features": (run_features_status, "show feature flag state"),
      ...
  }
  # kairix/agents/mcp/server.py — registered inside build_server()
  @server.tool()
  @async_tool_handler
  def features_status() -> dict[str, Any]:
      return tool_features_status()

Forbidden example:
  # kairix/cli.py COMMANDS has no 'features' entry; operators can only
  # read flag state by grep-ing the registry source. MCP also has no
  # @server.tool() features_status — agents have no programmatic
  # surface either, so a `features_status` MCP call is tool-not-found."""


# The CLI dispatch-wiring identifier(s) to read. Post-PLA-319 the literal is
# ``_CLI_HANDLERS`` and ``COMMANDS`` is DERIVED from it + the catalogue (no
# longer a dict literal); both are accepted so this reader stays correct across
# the pre-/post-derivation shapes.
_DISPATCH_NAMES = ("COMMANDS", "_CLI_HANDLERS")


def _dispatch_dict_literal(node: ast.AST) -> ast.Dict | None:
    """Return the dict literal assigned to a CLI dispatch-wiring name, else None.

    Recognises the ``_CLI_HANDLERS`` wiring literal and the legacy ``COMMANDS``
    literal, whether annotated (``AnnAssign``) or a plain ``Assign``.
    """
    if isinstance(node, ast.AnnAssign):
        targets: list[ast.expr] = [node.target]
    elif isinstance(node, ast.Assign):
        targets = list(node.targets)
    else:
        return None
    if not any(isinstance(t, ast.Name) and t.id in _DISPATCH_NAMES for t in targets):
        return None
    return node.value if isinstance(node.value, ast.Dict) else None


def _commands_has_features(cli_path: Path) -> bool:
    """Return True if kairix/cli.py's dispatch wiring has a 'features' key."""
    try:
        tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    for node in ast.walk(tree):
        dispatch = _dispatch_dict_literal(node)
        if dispatch is None:
            continue
        for key in dispatch.keys:
            if isinstance(key, ast.Constant) and key.value == "features":
                return True
    return False


# The registered MCP tool name that resolves to this capability. Post-PLA-318
# registration is catalogue-driven — ``build_server`` registers one tool per
# ``CAPABILITIES_CATALOG`` row, so ``features_status`` is registered iff the
# catalogue declares a ``mcp_tool="features_status"`` row (see ``_mcp_registry``).
_FEATURES_TOOL_NAME = "features_status"


def _mcp_registers_features_status(mcp_path: Path) -> bool:
    """Return True if server.py registers a ``features_status`` MCP tool.

    Reads the catalogue-driven registration: ``features_status`` is the
    ``mcp_tool`` of a ``_cap(...)`` row that ``build_server`` walks and
    registers, so the presence of that catalogue row IS the runtime MCP tool
    an agent calls.
    """
    return _FEATURES_TOOL_NAME in registered_mcp_tool_names(mcp_path)


def _features_module_available() -> bool:
    """Return True if kairix.core.features is importable (PR-2 has landed).

    Defensive import to detect PR-2 readiness; gate stays vacuous-green
    if absent.
    """
    try:
        import kairix.core.features  # noqa: F401 — presence-probe import, no symbol use
    except ImportError:
        return False
    return True


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 when surfaces are missing."""
    if not _features_module_available():
        print("ok [arch:f53-features-status-surface] — kairix.core.features absent; vacuous-green.")
        return 0

    cli_path = REPO_ROOT / CLI_REL_PATH
    mcp_path = REPO_ROOT / MCP_REL_PATH
    if not cli_path.exists() or not mcp_path.exists():
        print("ok [arch:f53-features-status-surface] — cli.py or server.py absent; vacuous-green.")
        return 0

    findings: list[str] = []
    if not _commands_has_features(cli_path):
        findings.append("kairix/cli.py:COMMANDS missing 'features' entry")
    if not _mcp_registers_features_status(mcp_path):
        findings.append("kairix/agents/mcp/server.py missing @server.tool() features_status")

    if not findings:
        print("ok [arch:f53-features-status-surface] — clean.")
        return 0

    print("FAIL [arch:f53-features-status-surface] — operator surface incomplete:")
    for finding in findings:
        print(f"  {finding}")
    print()
    print(REMEDIATION)
    return 1


if __name__ == "__main__":
    sys.exit(main())
