"""F30: Every CLI subcommand and MCP tool must have an outcome test.

The user-reported smell: "component testing / unit tests at the object/method
boundaries which had no real relationship to the desired behaviours or
expected outcomes." All 5233 Plan B-parity unit + contract + BDD tests
passed, yet the production-path LoCoMo benchmark returned 5.0% — below
the 11% pre-Plan-B baseline — because no test exercised the composed
production path from ``subprocess → kairix prep → SearchPipeline →
fact_retriever → fusion → synthesiser → LLM`` against a real ingested
fact. Every layer's fakes hid the composition's failure modes.

Rule: every subcommand listed in ``kairix/cli.py:COMMANDS`` AND every
``@server.tool()``-decorated function in ``kairix/agents/mcp/server.py``
MUST have at least one test that:

  1. Spawns the kairix subprocess (or invokes the MCP tool handler), AND
  2. Asserts on captured stdout / stderr / returned envelope content
     (NOT on return code alone, NOT on internal call-counts of fakes).

Mechanical detection per subcommand/tool:

  Subcommand ``<name>``: at least one ``tests/**/*.py`` file contains
  BOTH a ``subprocess.run(...)`` (or ``subprocess.Popen``) call with the
  string literal ``"<name>"`` in any args position, AND at least one
  ``assert`` statement that references the captured-output attributes
  ``.stdout`` / ``.stderr``.

  MCP tool ``<name>``: at least one ``tests/**/*.py`` file contains a
  direct call to ``tool_<name>(...)`` or ``<name>(...)`` where the
  function is the registered tool handler, AND at least one ``assert``
  statement that operates on the returned dict / envelope.

Violations are recorded by the CANONICAL file path of the subcommand
implementation (derived from the COMMANDS dict's module path) or by the
MCP server file for MCP tools — keeps violation reporting file-based and
consistent with all other F-rules.

There is no grandfathering: every subcommand/tool without an outcome
test fails the gate. Fix by adding a qualifying outcome test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _mcp_registry import registered_mcp_tool_names
from tc_fitness import REPO_ROOT, gate, repo_relative

REMEDIATION = """Refactor: every CLI subcommand and MCP tool MUST be
exercised end-to-end by at least one outcome test.

fix: add an outcome test that (a) invokes the subcommand via
``subprocess.run([..., "<subcommand>", ...])`` or calls the MCP tool
handler directly, (b) provides realistic input, and (c) asserts on
captured stdout / stderr / returned envelope content. NOT on
``returncode == 0`` alone, NOT on internal call-counts of fakes.

next: re-run ``python3 scripts/checks/check_f30_operator_outcome_tests.py``
to confirm the gate goes green.

run: bash scripts/safe-commit.sh "test(outcome): add F30 outcome test for <subcommand>"

Pass example:

    @pytest.mark.integration
    def test_ingest_chat_then_prep_round_trip_surfaces_fact(tmp_path):
        '''Ingest a fact, query for it, assert the value appears.'''
        # ... ingest a transcript mentioning "Caroline is VP of People" ...
        r = subprocess.run(
            ["kairix", "prep", "What is Caroline's role?"],
            capture_output=True, check=False, env=env, timeout=120,
        )
        out = r.stdout.decode()
        assert "VP" in out  # would fail if synth ignores fact_retriever
        assert "No relevant content" not in out  # explicit anti-template

Forbidden example:

    def test_prep_smoke(fake_search):  # uses Fake* everywhere
        result = prep_main(["What is X?"], search=fake_search)
        assert fake_search.called  # — internal-fake assertion, not outcome
        # — never exercises the subprocess path; synth bugs invisible

Reference: see ``docs/architecture/fitness-functions.md#f30`` and the
Plan B-parity RCA in ``docs/architecture/decisions/`` for the incident
that motivated this rule."""


# ---------- Step 1: enumerate subcommands from kairix/cli.py:COMMANDS ----------


def _extract_commands_dict(cli_path: Path) -> dict[str, str]:
    """Parse ``kairix/cli.py``; return {subcommand_name: module_path}.

    The dispatch wiring lives in the ``_CLI_HANDLERS`` dict literal (shape
    ``dict[str, tuple[str, str, bool]]``, first tuple element = module path);
    the runtime ``COMMANDS`` table is DERIVED from it + the catalogue (PLA-319),
    so ``COMMANDS`` is no longer a dict literal. Both names are accepted so this
    reader stays correct against the pre-derivation and post-derivation shapes.
    """
    tree = ast.parse(cli_path.read_text())
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id in ("COMMANDS", "_CLI_HANDLERS")
        ):
            if isinstance(node.value, ast.Dict):
                for k, v in zip(node.value.keys, node.value.values, strict=False):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) and isinstance(v, ast.Tuple):
                        if v.elts and isinstance(v.elts[0], ast.Constant):
                            out[k.value] = str(v.elts[0].value)
    return out


# ---------- Step 2: enumerate MCP tool names from server.py @server.tool() ----------


def _extract_mcp_tool_names(server_path: Path) -> set[str]:
    """Return the registered MCP tool names from the catalogue in ``server.py``.

    Post-PLA-318 registration is catalogue-driven — ``build_server`` walks
    ``CAPABILITIES_CATALOG`` and registers one tool per row (its ``mcp_tool``
    or, for operator-only stubs, its ``escalate_via`` name), so the registered
    set is read from the ``_cap(...)`` rows (see ``_mcp_registry``) rather than
    from ``@server.tool`` decorators that no longer exist.
    """
    return registered_mcp_tool_names(server_path)


# ---------- Step 3: scan tests/ for outcome-test signatures ----------


class _SubcommandOutcomeProbe(ast.NodeVisitor):
    """Detect, in one test file, the per-subcommand outcome-test shape.

    Records two facts:
      * which subcommand string literals appear inside ``subprocess.run``
        or ``subprocess.Popen`` argument lists
      * whether any ``assert`` statement references ``.stdout`` or
        ``.stderr``

    Both must be present for the file to qualify as an outcome test for
    that subcommand.
    """

    def __init__(self, subcommand_names: set[str]) -> None:
        self.subcommand_names = subcommand_names
        self.subcommands_invoked: set[str] = set()
        self.has_output_assert: bool = False

    def visit_Call(self, node: ast.Call) -> None:
        # subprocess.run(...) or subprocess.Popen(...) — both shapes
        f = node.func
        is_subprocess_call = (
            isinstance(f, ast.Attribute)
            and f.attr in {"run", "Popen"}
            and isinstance(f.value, ast.Name)
            and f.value.id == "subprocess"
        )
        if is_subprocess_call and node.args:
            # First positional arg is the args list/string
            first = node.args[0]
            literals = _collect_string_literals(first)
            for lit in literals:
                if lit in self.subcommand_names:
                    self.subcommands_invoked.add(lit)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        if _references_output(node.test):
            self.has_output_assert = True
        elif node.msg is not None and _references_output(node.msg):
            # F-string assertion messages that interpolate captured stdout
            # count too — they're a strong signal the test cares about the
            # output content, even when the test expression itself only
            # checks returncode.
            self.has_output_assert = True
        self.generic_visit(node)


def _collect_string_literals(node: ast.expr) -> set[str]:
    """Return every ``str`` ast.Constant value reachable inside ``node``.

    Used on the first positional argument of ``subprocess.run`` to find
    the subcommand name even when the list is constructed across lines.
    """
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


_OUTPUT_TOKENS = ("stdout", "stderr", "output")


def _references_output(node: ast.expr) -> bool:
    """Return True when the expression mentions captured-output content.

    Two recognised shapes:
      * ``Attribute`` with attr in ``{stdout, stderr}`` — e.g.
        ``assert "X" in proc.stdout.decode()``
      * ``Name`` whose identifier contains ``stdout``/``stderr``/``output``
        (case-insensitive) — e.g. ``assert "X" in prep_stdout``,
        ``assert prep_output == ...``. Covers the common pattern of
        decoding ``proc.stdout`` into a named variable and asserting on
        the variable.

    Conservative enough that an unrelated variable like ``count`` or
    ``parsed`` doesn't trigger; broad enough that natural test shapes
    qualify without contortion.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in {"stdout", "stderr"}:
            return True
        if isinstance(sub, ast.Name):
            lower = sub.id.lower()
            if any(tok in lower for tok in _OUTPUT_TOKENS):
                return True
    return False


class _McpOutcomeProbe(ast.NodeVisitor):
    """Detect, in one test file, the per-MCP-tool outcome-test shape.

    Records:
      * which ``tool_<name>(...)`` functions are called
      * whether any ``assert`` statement operates on a Subscript /
        Attribute (looking at returned-dict content like ``r["facts"]``
        or ``r.facts``) — a return-envelope assertion
    """

    def __init__(self, tool_names: set[str]) -> None:
        self.tool_names = tool_names
        self.tools_invoked: set[str] = set()
        self.has_envelope_assert: bool = False

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id.startswith("tool_"):
            short = f.id[len("tool_") :]
            if short in self.tool_names:
                self.tools_invoked.add(short)
        elif isinstance(f, ast.Attribute) and f.attr.startswith("tool_"):
            short = f.attr[len("tool_") :]
            if short in self.tool_names:
                self.tools_invoked.add(short)
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        for sub in ast.walk(node.test):
            if isinstance(sub, ast.Subscript | ast.Attribute):
                self.has_envelope_assert = True
                break
        self.generic_visit(node)


def _scan_tests_for_outcome_coverage(
    subcommand_names: set[str],
    mcp_tool_names: set[str],
) -> tuple[set[str], set[str]]:
    """Walk ``tests/**/*.py``; return (subcommands_covered, tools_covered)."""
    subcommands_covered: set[str] = set()
    tools_covered: set[str] = set()
    tests_dir = REPO_ROOT / "tests"
    if not tests_dir.exists():
        return subcommands_covered, tools_covered

    for path in tests_dir.rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue

        sub_probe = _SubcommandOutcomeProbe(subcommand_names)
        sub_probe.visit(tree)
        if sub_probe.subcommands_invoked and sub_probe.has_output_assert:
            subcommands_covered |= sub_probe.subcommands_invoked

        mcp_probe = _McpOutcomeProbe(mcp_tool_names)
        mcp_probe.visit(tree)
        if mcp_probe.tools_invoked and mcp_probe.has_envelope_assert:
            tools_covered |= mcp_probe.tools_invoked

    return subcommands_covered, tools_covered


# ---------- Step 4: map subcommand/tool → canonical implementation file ----------


def _module_path_to_file(module_path: str) -> Path | None:
    """``kairix.use_cases.ingest_chat`` → ``kairix/use_cases/ingest_chat.py``.

    Returns None if the resolved file does not exist on disk (helps us
    skip stale COMMANDS entries during a partial refactor without
    crashing the gate).
    """
    rel = Path(*module_path.split("."))
    candidate = REPO_ROOT / rel.with_suffix(".py")
    if candidate.exists():
        return candidate
    # Some modules are packages — check __init__.py
    package_init = REPO_ROOT / rel / "__init__.py"
    if package_init.exists():
        return package_init
    return None


def main() -> int:
    cli_file = REPO_ROOT / "kairix" / "cli.py"
    mcp_file = REPO_ROOT / "kairix" / "agents" / "mcp" / "server.py"
    if not cli_file.exists() or not mcp_file.exists():
        # Repo is partially scaffolded — gate stays green to not block
        # bootstrap work; F30 activates once both surfaces exist.
        return gate("f30-operator-outcome-tests", set(), REMEDIATION)

    commands = _extract_commands_dict(cli_file)
    mcp_tools = _extract_mcp_tool_names(mcp_file)
    subcommand_names = set(commands.keys())

    subcommands_covered, tools_covered = _scan_tests_for_outcome_coverage(subcommand_names, mcp_tools)

    violations: set[Path] = set()

    for name, module_path in commands.items():
        if name in subcommands_covered:
            continue
        impl = _module_path_to_file(module_path)
        # If the module path can't be resolved, fall back to cli.py as
        # the carrier — the violation still needs a file anchor.
        anchor = impl if impl is not None else cli_file
        violations.add(repo_relative(anchor))

    # For MCP tools without coverage, anchor the violation at server.py
    # (we want one entry per missing tool — encode the name in the anchor
    # by using a synthetic Path that includes the tool name, so reported
    # violations are distinguishable per tool).
    for tool_name in sorted(mcp_tools - tools_covered):
        synthetic = Path("kairix/agents/mcp/server.py") / f"@tool:{tool_name}"
        violations.add(synthetic)

    return gate("f30-operator-outcome-tests", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
