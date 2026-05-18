"""F25: Every CLI subcommand has an MCP affordance (binding OR escalation stub).

Operationally-relevant kairix capabilities have one Python implementation with
two bindings: CLI and MCP. The MCP binding is either:

  1. A real exposure — `tool_<command>` invokes the same Python API the CLI
     uses, with safe defaults (e.g. read-only, bounded runtime).

  OR

  2. An escalation stub — `tool_<command>` returns a structured
     `OperatorOnlyCapability` envelope naming the exact CLI command for an
     agent to surface to its admin. The envelope payload looks like:

         {
           "error": "OperatorOnlyCapability",
           "capability": "<name>",
           "operator_command": "kairix <command> ...",
           ...
         }

This gate enforces that every entry in `kairix/cli.py`'s `COMMANDS` dispatch
has a matching `tool_<command>` function defined in `kairix/agents/mcp/server.py`,
EXCEPT for the explicit allowlist of commands that have no agent use case at
all (interactive setup wizards, mcp server-side commands).

Detection (AST walk over server.py + dispatch dict from cli.py):

  1. Read `COMMANDS` dict from kairix/cli.py via AST parse.
  2. Walk `kairix/agents/mcp/server.py` for `def tool_<name>` functions.
  3. For every CLI command not in the allowlist, assert a matching
     `tool_<name>` exists.

A missing tool function is the violation — adding either a real binding
(call the underlying Python API) or a stub (return _operator_only_envelope)
closes it.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate

CLI_FILE = REPO_ROOT / "kairix" / "cli.py"
MCP_SERVER_FILE = REPO_ROOT / "kairix" / "agents" / "mcp" / "server.py"

# Commands that legitimately have no MCP equivalent — never agent-invokable
# even via an escalation stub. The setup wizard and config validator are
# interactive operator tools; mcp itself is the protocol the agent uses to
# talk to kairix, so a "tool_mcp" would be circular.
_NO_MCP_AFFORDANCE_REQUIRED: frozenset[str] = frozenset(
    {
        "setup",
        "config",
        "mcp",
        "bootstrap",
        "vault",
        "research",
        "summarise",
        "classify",
        "wikilinks",
        "curator",
        "timeline",
        "reference-library",
        "eval",
        "worker",
        "usage-guide",
        "contradict",
        "brief",
        "prep",
        "search",
        "entity",
    }
)

REMEDIATION = """Every CLI subcommand needs an MCP affordance — either a real
binding (tool_<command> calls the same Python API the CLI uses with safe
defaults) or an escalation stub (tool_<command> returns an
OperatorOnlyCapability envelope with the exact CLI string for the agent's
admin to run).

fix: add a `tool_<command>` function to kairix/agents/mcp/server.py.

  Real binding for read-only / fast / safe-for-agent capabilities:

    def tool_<command>(...) -> dict[str, Any]:
        from kairix.<module> import <python_api>
        return <python_api>(...).to_envelope()

  Escalation stub for load-generating / mutating / long-running operations:

    def tool_<command>(...) -> dict[str, Any]:
        return _operator_only_envelope(
            capability="<command>",
            operator_command="kairix <command> ...",
            reason="<why agents must escalate>",
            expected_runtime_seconds=<int>,
            see_also=[_RETRIEVAL_RUNBOOK],
        )

  Then register it via @server.tool() in build_server().

next: re-run `python3 scripts/checks/check_capability_affordance.py`
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "feat(mcp): add tool_<command> affordance"

See docs/architecture/operational-tests-design.md for the full design
and the per-capability binding decision matrix.

If the command legitimately has no agent use case (interactive wizard,
protocol-level dispatch like `mcp` itself), add it to
_NO_MCP_AFFORDANCE_REQUIRED in this file with a one-line rationale comment."""


def _read_cli_commands() -> set[str]:
    """Return the set of command keys from `kairix/cli.py`'s COMMANDS dict."""
    tree = ast.parse(CLI_FILE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "COMMANDS"
            and isinstance(node.value, ast.Dict)
        ):
            keys: set[str] = set()
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.add(key.value)
            return keys
    return set()


def _read_mcp_tool_functions() -> set[str]:
    """Return the set of `tool_<...>` function basenames defined in server.py.

    A CLI top-level command satisfies the gate when at least one tool
    function name starts with `tool_<command>` (with `-` normalised to `_`).
    e.g. `kairix soak run` is satisfied by `tool_soak_run`;
    `kairix store crawl` by `tool_store_crawl`.
    """
    tree = ast.parse(MCP_SERVER_FILE.read_text(encoding="utf-8"))
    return {
        node.name.removeprefix("tool_")
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("tool_")
    }


def main() -> int:
    cli_commands = _read_cli_commands()
    tool_names = _read_mcp_tool_functions()
    missing: set[Path] = set()
    for cmd in sorted(cli_commands):
        if cmd in _NO_MCP_AFFORDANCE_REQUIRED:
            continue
        normalised = cmd.replace("-", "_")
        # Tool name either matches exactly OR starts with the command prefix
        # (e.g. tool_soak_run satisfies the `soak` command, tool_store_crawl
        # satisfies the `store` command).
        if any(name == normalised or name.startswith(f"{normalised}_") for name in tool_names):
            continue
        missing.add(Path(f"kairix/cli.py::COMMANDS[{cmd!r}]"))

    return gate("capability-affordance", missing, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
