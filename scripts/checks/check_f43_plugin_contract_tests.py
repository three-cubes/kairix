"""F43: behavioural parity — one parametrized contract-test body run over
the real implementation AND the canonical fake (not separate assertions).

F43 has two limbs, both reported through the single ``f43`` gate:

Limb 1 — plugin contract-test presence (original shape)
-------------------------------------------------------
Plugins under ``kairix/connectors/<name>/``, ``kairix/extractors/<name>/``,
and ``kairix/providers/<name>/`` are independently shippable. Every
plugin must carry ``tests/contracts/test_<name>_protocol.py`` that
imports BOTH the canonical fake from ``tests.fakes`` AND the real
implementation from ``kairix.<tree>.<name>``.

Limb 2 — behavioural parity (the strengthening, #499 phase 1)
-------------------------------------------------------------
Importing both impls is not enough: a contract test where the real and
the fake are checked by SEPARATE test functions (one real-only ``raises``
test, one fake-only assertion) lets the two drift apart while every suite
stays green. That is exactly how session-escape 7 slipped through —
``FakeSetupService`` modelled an empty corpus as *instantly done* while
the real ``KairixSetupService`` spins forever, and because fake and real
were proved by different bodies the inversion was invisible.

So every contract-test FUNCTION in ``tests/contracts/test_*_protocol.py``
AND ``tests/contracts/test_*_failure_modes.py`` must run its assertions
over ≥2 implementations through ONE shared body — detected as either:

  * ``@pytest.mark.parametrize`` over an implementation parameter
    (``factory`` / ``impl`` / ``subject`` / ``backend`` / ``service`` /
    ``provider`` / ``connector`` / ``sut`` / ``under_test`` / ``name``)
    with ≥2 argvalues, OR
  * a test parameter fed by a module-level ``@pytest.fixture(params=[…])``
    carrying ≥2 params (an indirectly-parametrized real+fake fixture), OR
  * a ``# F43-single-impl: <why>`` rationale line on the function (the
    escape hatch for genuinely single-impl Protocol probes — e.g. a
    fake-only failure-injection knob with no real-side analogue).

A file is a Limb-2 violation when ANY of its ``test_*`` functions fails
all three. There is no grandfathering: every contract file must use the
parametrized real+fake body.

Violations from both limbs are reported by repo-relative path through
the single ``f43`` gate. If no plugin trees / contract
files exist on disk, the check passes trivially.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

_PLUGIN_TREES_REL: tuple[Path, ...] = (
    Path("kairix") / "connectors",
    Path("kairix") / "extractors",
    Path("kairix") / "providers",
)

_NON_PLUGIN_NAMES: frozenset[str] = frozenset({"__pycache__"})

_CONTRACTS_DIR_REL = Path("tests") / "contracts"

# Parameter names that signal "this argument is the implementation under
# test" — a parametrize over one of these (with ≥2 values) is the
# real+fake parity shape F43 requires.
_IMPL_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "factory",
        "impl",
        "implementation",
        "subject",
        "backend",
        "service",
        "provider",
        "connector",
        "extractor",
        "sut",
        "under_test",
        "candidate",
        "name",
    }
)

# A function carrying this marker is exempt from the parity requirement —
# the single-impl Protocol probe escape hatch (F21 rationale shape).
_SINGLE_IMPL_MARKER = "# F43-single-impl:"

REMEDIATION = """F43 has two limbs:

LIMB 1 — a plugin is missing ``tests/contracts/test_<name>_protocol.py``,
OR the file exists but doesn't import BOTH the canonical fake from
``tests/fakes.py`` AND the real implementation from
``kairix/<tree>/<name>/``.

LIMB 2 — a contract-test FUNCTION in ``tests/contracts/test_*_protocol.py``
or ``test_*_failure_modes.py`` proves the real impl and the fake with
SEPARATE bodies instead of ONE parametrized body run over both. A real-only
``raises`` test beside a fake-only assertion is the forbidden shape:
fake and real can invert the same contract (session-escape 7: the fake
reported an empty corpus as instantly-done while the real backend spins
forever) and every suite still passes, because no single body ever ran
the same assertion over both.

fix: (Limb 1) create ``tests/contracts/test_<name>_protocol.py`` that
imports the canonical fake AND the real implementation.
fix: (Limb 2) collapse the real-only + fake-only tests into ONE body
parametrized over both impls — ``@pytest.mark.parametrize`` over an
implementation param (``factory`` / ``service`` / ``backend`` / …) with
≥2 values, OR a ``@pytest.fixture(params=[real, fake])``. Genuinely
single-impl probes carry a ``# F43-single-impl: <why>`` rationale line.
next: re-run python3 scripts/checks/check_f43_plugin_contract_tests.py
to confirm the gate goes green.
run: bash scripts/safe-commit.sh \"test(contracts): parity body for <plugin>\"

Pass example: (one body, run over real + fake — proves both agree)
  import pytest
  from kairix.platform.setup.service import build_setup_service
  from tests.fakes import FakeSetupService

  @pytest.mark.contract
  @pytest.mark.parametrize(
      \"service\",
      [build_setup_service(deps=...), FakeSetupService(config_file=\"x\")],
  )
  def test_config_file_path_is_non_empty_when_resolvable(service):
      assert service.config_file_path() != \"\"   # SAME assertion, both impls

Forbidden example: (separate bodies — real and fake never co-asserted)
  def test_config_file_path_real():
      assert build_setup_service(deps=...).config_file_path() != \"\"

  def test_config_file_path_fake():
      assert FakeSetupService(config_file=\"\").config_file_path() == \"\"
  # the fake's empty-semantics inversion never meets the real assertion

Why: F30 proves the CLI / MCP entry points compose against real code;
F43 proves the plugin/Protocol layer's fake stays behaviourally faithful
to the real wire by forcing the SAME assertion through both. Together
they remove the LoCoMo-class blind spot where unit + BDD tests all pass
against fakes that have quietly drifted from real behaviour."""


def _discover_plugins(tree_root: Path) -> list[Path]:
    """List plugin directories under ``tree_root``.

    Skips ``_``-prefixed names and the cache allow-list. Files at
    the tree root are never plugins. Empty list if the root doesn't
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


def _file_imports_both(contract_file: Path, plugin_dotted: str) -> bool:
    """True if ``contract_file`` imports from both ``tests.fakes`` AND
    from the real plugin module path (``plugin_dotted``, e.g.
    ``kairix.providers.openai``).

    Recognises every common import shape:

      * ``from tests.fakes import X``
      * ``from tests import fakes``
      * ``import tests.fakes``
      * ``from kairix.providers.openai import OpenAIProvider``
      * ``import kairix.providers.openai``
      * ``from kairix.providers.openai.provider import OpenAIProvider``
    """
    try:
        tree = ast.parse(contract_file.read_text(encoding="utf-8"), filename=str(contract_file))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False

    imports_fakes = False
    imports_real = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "tests.fakes" or module.startswith("tests.fakes."):
                imports_fakes = True
            elif module == "tests" and any(alias.name == "fakes" for alias in node.names):
                imports_fakes = True
            if module == plugin_dotted or module.startswith(plugin_dotted + "."):
                imports_real = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name == "tests.fakes" or name.startswith("tests.fakes."):
                    imports_fakes = True
                if name == plugin_dotted or name.startswith(plugin_dotted + "."):
                    imports_real = True

    return imports_fakes and imports_real


def _plugin_violates(plugin_dir: Path, repo_root: Path) -> bool:
    """True if the plugin lacks
    ``tests/contracts/test_<name>_protocol.py`` OR the file exists but
    doesn't carry both the fake and real imports.
    """
    name = plugin_dir.name
    contract_file = repo_root / _CONTRACTS_DIR_REL / f"test_{name}_protocol.py"
    if not contract_file.is_file():
        return True
    # Build the dotted production module path: kairix.<tree>.<name>
    # (e.g. kairix.providers.openai). The plugin_dir is
    # ``<repo_root>/kairix/<tree>/<name>``; resolving relative to
    # ``repo_root`` avoids the worktree-bug where the filesystem path
    # itself contains ``kairix`` as a parent directory (e.g.
    # ``/work/kairix/kairix/kairix/extractors/<name>``) — using
    # ``parts.index('kairix')`` on the absolute path picks the wrong
    # occurrence and the dotted lookup mismatches every legitimate
    # contract-test import.
    try:
        rel_parts = plugin_dir.resolve().relative_to(repo_root.resolve()).parts
    except ValueError:
        return True
    if not rel_parts or rel_parts[0] != "kairix":
        return True
    plugin_dotted = ".".join(rel_parts)
    return not _file_imports_both(contract_file, plugin_dotted)


# ----------------------------------------------------------------------
# Limb 2 — behavioural parity (one parametrized body over real + fake)
# ----------------------------------------------------------------------


def _parametrize_argnames(decorator: ast.Call) -> list[str]:
    """The parameter names declared by a ``@pytest.mark.parametrize``
    decorator's first positional arg.

    ``parametrize("service", …)`` → ``["service"]``;
    ``parametrize("name,factory", …)`` → ``["name", "factory"]``;
    ``parametrize(("a", "b"), …)`` → ``["a", "b"]``. Anything else
    (a computed argname) yields ``[]``.
    """
    if not decorator.args:
        return []
    spec = decorator.args[0]
    if isinstance(spec, ast.Constant) and isinstance(spec.value, str):
        return [part.strip() for part in spec.value.split(",") if part.strip()]
    if isinstance(spec, (ast.List, ast.Tuple)):
        names: list[str] = []
        for elt in spec.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                names.append(elt.value.strip())
        return names
    return []


def _parametrize_value_count(decorator: ast.Call) -> int:
    """How many cases the parametrize's second positional arg supplies.

    Only literal ``list`` / ``tuple`` argvalues are counted directly; a
    name reference (``_FACTORIES``) returns ``-1`` meaning "indeterminate
    — trust it" (the canonical provider/connector shape passes a
    module-level ``_FACTORIES`` list of ≥2 real+fake factories)."""
    if len(decorator.args) < 2:
        return -1
    values = decorator.args[1]
    if isinstance(values, (ast.List, ast.Tuple)):
        return len(values.elts)
    # Name / call / comprehension — can't count statically; trust it.
    return -1


def _is_parametrize_decorator(decorator: ast.expr) -> ast.Call | None:
    """Return the ``Call`` node iff ``decorator`` is
    ``@pytest.mark.parametrize(...)`` (or ``@…parametrize(...)``)."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if isinstance(func, ast.Attribute) and func.attr == "parametrize":
        return decorator
    return None


def _fn_has_impl_parametrize(fn: ast.FunctionDef) -> bool:
    """True iff ``fn`` carries an ``@pytest.mark.parametrize`` over an
    implementation parameter with ≥2 cases (or an indeterminate
    module-level value list, which the canonical shape uses)."""
    for decorator in fn.decorator_list:
        call = _is_parametrize_decorator(decorator)
        if call is None:
            continue
        argnames = _parametrize_argnames(call)
        if not any(name in _IMPL_PARAM_NAMES for name in argnames):
            continue
        count = _parametrize_value_count(call)
        if count == -1 or count >= 2:
            return True
    return False


def _parametrized_fixture_params(tree: ast.Module) -> set[str]:
    """Names of module-level ``@pytest.fixture(params=[…])`` functions
    whose ``params`` carry ≥2 cases — the indirectly-parametrized
    real+fake fixture shape. A test taking one of these as a parameter
    is run over both impls."""
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and func.attr == "fixture"):
                continue
            for kw in decorator.keywords:
                if kw.arg != "params":
                    continue
                if isinstance(kw.value, (ast.List, ast.Tuple)) and len(kw.value.elts) >= 2:
                    out.add(node.name)
    return out


def _fn_uses_parametrized_fixture(fn: ast.FunctionDef, fixture_names: set[str]) -> bool:
    """True iff ``fn`` takes one of ``fixture_names`` as a parameter."""
    return any(arg.arg in fixture_names for arg in fn.args.args)


def _fn_has_single_impl_rationale(fn: ast.FunctionDef, source_lines: list[str]) -> bool:
    """True iff a ``# F43-single-impl:`` rationale attaches to ``fn`` — in
    the contiguous comment block immediately above it, on its
    decorator/signature lines, or on its first body line.

    The escape hatch for genuinely single-impl Protocol probes (e.g. a
    fake-only failure-injection knob with no real-side analogue)."""
    # Span from the first decorator (or def) line to the first body
    # statement's line, 1-indexed inclusive — the function's own lines.
    start = fn.decorator_list[0].lineno if fn.decorator_list else fn.lineno
    end = fn.body[0].lineno if fn.body else fn.lineno
    for lineno in range(start, end + 1):
        idx = lineno - 1
        if 0 <= idx < len(source_lines) and _SINGLE_IMPL_MARKER in source_lines[idx]:
            return True
    # Walk upward over the contiguous comment / blank block directly
    # above the function — the idiomatic placement for a rationale that
    # introduces the test. Stop at the first non-comment, non-blank line.
    idx = start - 2  # line directly above `start` (0-indexed)
    while idx >= 0:
        stripped = source_lines[idx].strip()
        if not stripped:
            idx -= 1
            continue
        if not stripped.startswith("#"):
            break
        if _SINGLE_IMPL_MARKER in source_lines[idx]:
            return True
        idx -= 1
    return False


def _contract_file_violates_parity(contract_file: Path) -> bool:
    """True iff any ``test_*`` function in ``contract_file`` is proved
    against a single implementation — neither parametrized over an impl
    param, nor fed by a parametrized real+fake fixture, nor carrying a
    ``# F43-single-impl:`` rationale.
    """
    try:
        source = contract_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(contract_file))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return False
    source_lines = source.splitlines()
    fixture_names = _parametrized_fixture_params(tree)

    def _test_fns(body: list[ast.stmt]) -> list[ast.FunctionDef]:
        fns: list[ast.FunctionDef] = []
        for node in body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                fns.append(node)
            elif isinstance(node, ast.ClassDef):
                fns.extend(_test_fns(node.body))
        return fns

    for fn in _test_fns(tree.body):
        if _fn_has_impl_parametrize(fn):
            continue
        if _fn_uses_parametrized_fixture(fn, fixture_names):
            continue
        if _fn_has_single_impl_rationale(fn, source_lines):
            continue
        return True
    return False


def _discover_contract_files(repo_root: Path) -> list[Path]:
    """``tests/contracts/test_*_protocol.py`` AND ``test_*_failure_modes.py``
    — the files that prove a Protocol's real+fake parity."""
    contracts_dir = repo_root / _CONTRACTS_DIR_REL
    if not contracts_dir.is_dir():
        return []
    out: list[Path] = []
    for pattern in ("test_*_protocol.py", "test_*_failure_modes.py"):
        out.extend(sorted(contracts_dir.glob(pattern)))
    return out


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Both F43 limbs, as one repo-relative violation set reported
    through the single ``f43`` gate.

    Limb 1 — plugin dirs missing an F43-conformant contract test.
    Limb 2 — contract files proving a Protocol against a single impl
    instead of a parametrized real+fake body.

    Empty set if neither plugin trees nor contract files exist.
    """
    violations: set[Path] = set()
    # Limb 1 — plugin presence + dual imports.
    for tree_rel in _PLUGIN_TREES_REL:
        tree_root = repo_root / tree_rel
        for plugin_dir in _discover_plugins(tree_root):
            if _plugin_violates(plugin_dir, repo_root):
                try:
                    violations.add(plugin_dir.resolve().relative_to(repo_root))
                except ValueError:
                    violations.add(tree_rel / plugin_dir.name)
    # Limb 2 — behavioural parity in the contract bodies.
    for contract_file in _discover_contract_files(repo_root):
        if _contract_file_violates_parity(contract_file):
            try:
                violations.add(contract_file.resolve().relative_to(repo_root))
            except ValueError:
                violations.add(_CONTRACTS_DIR_REL / contract_file.name)
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f43", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
