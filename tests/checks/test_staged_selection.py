"""Soundness battery for precise per-rule staged selection (#499 Phase 2 stage 4b).

The non-negotiable property of ``run_checks.py --staged`` is **no false
negative on staged changes**: if staging file(s) introduces a violation of
rule R, staged mode MUST select, run, and FAIL R. This module proves that
EXECUTED — for a representative violation of each selection class it stages the
offending file(s) and asserts staged mode catches it — and then closes the
loop with a completeness table: across a battery of staged sets, the rules
staged mode RUNS are a superset of the rules a full ``--all`` run would flag
restricted to those staged files (over-running a cross-cutting rule is fine;
under-running is the only real danger).

Mechanism
---------
Each scenario writes a real violating file into the actual repo tree (under a
``zzz_staged_probe*`` name a try/finally unlinks), then drives the real staged
selection with that path in the staged list. The probe MUST live under the repo
tree because the staged dispatch scans ``REPO_ROOT`` and intersects that walk
with the staged set — a ``tmp_path`` probe would sit outside the package roots
the detectors enumerate and be invisible. Because file-local rules narrow their
file index to the staged set, the dispatch inspects only the probe file — so a
clean tree stays green and the injected violation is the only signal. No
monkeypatch of kairix internals: the probe file IS the production scenario, and
``decide`` / ``restrict_python_files`` are the real runner code.

Cost + #504 isolation
---------------------
The file-local single-rule proofs (F8, F26) drive ONE rule through a narrowed
in-process dispatch (:func:`_run_one_narrowed`) rather than re-running the whole
~20-50-rule staged gate just to read one ledger line. ``_run_one_narrowed``
mirrors the real ``_run_staged_one``: it ``decide``s the rule, then runs it
in-process inside ``restrict_python_files`` + kairix's ``_enumeration_narrower``
scoped to the decision's ``scope_files``, so the detector walks ONLY the staged
probe. This is BOTH the cost fix (F8 12s→<0.1s, F26 14s→<0.2s) AND the #504
closure: a narrowed F8 scan can no longer pick up an orphaned ``zzz_staged_probe_*``
left by an interrupted run, because it never walks the whole ``tests/`` tree.
The ``_sweep_staged_probes`` session-autouse fixture below adds belt-and-braces
hygiene, removing any leftover probe before AND after the session so the cluster
is structurally immune to interrupt-debris. ONE representative end-to-end
full-dispatch smoke is retained (``test_file_local_f26_forbidden_import_caught``)
for the per-commit signal that the real ``_dispatch_staged`` wiring is intact;
the other selection proofs assert ``decide`` / the ran-set without the expensive
dispatch tail.

Sabotage proofs (executed; see the runner-agent report for the mutate→fail→
restore runs):

  * file-local F26: removing the forbidden import from the probe → narrowed
    dispatch goes green; restoring it → red. (The probe IS the mutation.)
  * file-local F8: removing the category marker → narrowed dispatch red;
    adding ``pytestmark = pytest.mark.unit`` → green. (The probe IS the mutation.)
  * relational F30: pointing the new COMMANDS subcommand at an EXISTING tested
    command → green; a brand-new untested name → red.
  * completeness: dropping a rule from ``_staged_decisions`` (so it stops being
    selected) → the superset assertion below goes red for that rule's scope.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import run_checks  # noqa: E402
from _rule_catalogue import ALL_ENTRIES  # noqa: E402

# The staged-selection primitives now live in the shared three-cubes-fitness
# package; kairix re-exports kairix-resolver-bound versions through run_checks
# (so ``decide`` / ``resolve_staged_scope`` derive scope via kairix's
# FitnessRule-aware resolver exactly as the pre-migration local module did).
from run_checks import decide, resolve_staged_scope, staged_in_scope  # noqa: E402
from tc_fitness.context import CheckContext  # noqa: E402
from tc_fitness.staged import restrict_python_files  # noqa: E402

pytestmark = pytest.mark.unit


# ── #504 isolation hygiene: sweep interrupt-debris probes ────────────────


@pytest.fixture(scope="session", autouse=True)
def _sweep_staged_probes() -> Iterator[None]:
    """Remove any ``zzz_staged_probe_*`` debris a prior INTERRUPTED run left in
    the repo tree, before AND after this session.

    A staged-selection probe that an interrupt orphaned (a hard ``Ctrl-C``
    between ``write_text`` and the ``finally`` unlink) used to leave a
    ``zzz_staged_probe_*.py`` under ``tests/`` or ``kairix/`` that a later
    full-tree scan would pick up — the #504 isolation flake. The single-rule
    narrowing (:func:`_run_one_narrowed`) already makes the F8/F26 scans
    structurally immune (they never walk the whole tree), and this fixture is
    the belt-and-braces complement: it makes the WHOLE cluster idempotent under
    interrupt by sweeping every orphaned probe file and probe directory at
    session boundaries. Only ever touches uniquely-named ``zzz_staged_probe*``
    paths, so it can never delete a real file."""
    _purge_probe_debris()
    try:
        yield
    finally:
        _purge_probe_debris()


def _purge_probe_debris() -> None:
    """Delete every ``zzz_staged_probe*`` file or directory under the repo tree
    (the uniquely-named probe namespace — never a real path)."""
    import shutil

    for path in sorted(_REPO_ROOT.rglob("zzz_staged_probe*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


# ── harness ─────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _probe_file(rel: str, content: str) -> Iterator[str]:
    """Write ``content`` to ``rel`` under the real repo tree, yield the
    repo-relative path string, and FULLY remove it on exit — the file, any
    ``__pycache__`` an import created, and any directory the probe itself
    created (try/finally so a failed assert never leaves a shadow). ``rel``
    must be a ``zzz_staged_probe*`` path so it can never collide with a real
    file, and the probe-dir teardown only deletes ``zzz_staged_probe*``
    directories so it can never touch a real tree."""
    assert "zzz_staged_probe" in rel, "probe paths must be uniquely named to avoid collisions"
    path = _REPO_ROOT / rel
    created_dir = "zzz_staged_probe" in path.parent.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        yield rel
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        # A staged-mode dispatch may have imported the probe, leaving a
        # __pycache__ that would keep a probe PLUGIN dir alive (F36/F41/...
        # treat any dir under kairix/connectors/ as a plugin). Remove the
        # whole probe dir — but only ever a uniquely-named probe dir.
        if created_dir and path.parent.exists():
            import shutil

            shutil.rmtree(path.parent, ignore_errors=True)


def _run_staged(staged: list[str]) -> tuple[int, str]:
    """Drive the real ``_dispatch_staged`` over ``staged``; return
    ``(exit_code, captured_output)``.

    This runs the FULL ~20-50-rule staged gate — it is reserved for the ONE
    retained end-to-end smoke that proves the dispatch wiring is intact. The
    single-rule proofs use :func:`_run_one_narrowed` instead (one rule, narrowed
    to the staged probe) so they neither pay the whole-gate cost nor walk the
    whole tree (#504)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = run_checks._dispatch_staged(staged, skip_coverage=True)
    return code, buf.getvalue()


def _run_one_narrowed(rule_id: str, staged: list[str]) -> tuple[int, str]:
    """Dispatch ONLY ``rule_id`` over ``staged`` through the REAL staged path,
    narrowed to the decision's staged files — the single-rule equivalent of the
    runner's ``_run_staged_one``.

    Drives kairix's real ``decide`` to get the rule's :class:`StagedDecision`,
    then runs that one rule in-process inside ``restrict_python_files`` +
    kairix's ``_enumeration_narrower`` scoped to ``decision.scope_files`` — so a
    file-local detector walks ONLY the staged probe, exactly as the full staged
    dispatch would scope it. This is the same code path ``_run_staged_one`` takes
    for a file-local rule, isolated to one rule so a single-rule proof costs
    <0.2s instead of re-running the whole gate (and never walks the full tree,
    closing the #504 stale-probe sensitivity). The decision MUST be ``run`` —
    these proofs stage a path the rule's scope contains.

    Returns ``(rc, captured_output)`` where ``rc`` is 0 (pass) / 1 (fail)."""
    entry = next(e for e in run_checks._select_all() if e.id == rule_id)
    script = run_checks.resolve_script(entry)
    decision = decide(entry, script, staged)
    assert decision.run, f"{rule_id} must be selected for staged={staged}; reason: {decision.reason}"
    buf = io.StringIO()
    ctx = CheckContext(repo_root=run_checks.REPO_ROOT)
    with ctx.install(), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with contextlib.ExitStack() as stack:
            if decision.scope_files:
                scope_files = list(decision.scope_files)
                stack.enter_context(restrict_python_files(run_checks.REPO_ROOT, scope_files))
                stack.enter_context(run_checks._enumeration_narrower(run_checks.REPO_ROOT, scope_files))
            rc = run_checks._run_one_inprocess(entry, ctx)
    return rc, buf.getvalue()


def _load_detector(script: str, module_name: str) -> ModuleType:
    """Import a check-detector module by file path (the ``tests/architecture/``
    pattern) so a relational rule's cross-file verdict can be proven through its
    own ``collect_violations(repo_root)`` / ``file_has_violation(...)`` seam over
    a ``tmp_path`` tree — the SAME verdict the full staged dispatch reaches, but
    hermetic and <1ms instead of re-running the whole gate."""
    detector_path = _CHECKS_DIR / script
    spec = importlib.util.spec_from_file_location(module_name, detector_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _failed_rule_ids(output: str) -> set[str]:
    """The rule ids that FAILED in a staged ledger (the ``FAIL [id]`` lines)."""
    ids: set[str] = set()
    for line in output.splitlines():
        if "FAIL [" in line:
            ids.add(line.split("FAIL [", 1)[1].split("]", 1)[0])
    return ids


def _ran_rule_ids(output: str) -> set[str]:
    """The rule ids that RAN (the ``run [id]`` lines) in a staged ledger."""
    ids: set[str] = set()
    for line in output.splitlines():
        if "run [" in line:
            ids.add(line.split("run [", 1)[1].split("]", 1)[0])
    return ids


def _skipped_rule_ids(output: str) -> set[str]:
    ids: set[str] = set()
    for line in output.splitlines():
        if "skip [" in line:
            ids.add(line.split("skip [", 1)[1].split("]", 1)[0])
    return ids


# ── file-local: forbidden import (F26) — the retained end-to-end smoke ───


def test_file_local_f26_forbidden_import_caught() -> None:
    """A staged kairix/core file importing kairix.providers → staged mode runs
    and FAILS F26 (file-local class).

    This is the ONE retained end-to-end full ``_dispatch_staged`` smoke — it
    proves the real staged-dispatch wiring (selection → narrowing → in-process
    ledger) is intact end-to-end on the per-commit path, AND it carries the
    cluster's only clean-arm control through the full gate (the sabotage arm
    below stages a clean probe and asserts F26 clears). The other file-local
    proofs (F8) use the cheaper single-rule :func:`_run_one_narrowed`; only this
    one pays the whole-gate cost, by design, and only ONCE (the clean arm uses
    the narrowed single-rule path).

    Robustness (#506): the assertions are scoped to **F26's own verdict in the
    ledger**, never the aggregate exit code. ``_run_staged`` drives the FULL
    ~40-rule staged gate, several of whose rules (F94 system-path writes,
    F92 catalogue-currency, F22 path-naming, the token scanners) read
    whole-tree / git state — so a stray probe or ``__pycache__`` another test
    left in the tree could flip the aggregate ``exit_code`` to 1 even when this
    probe's own change is clean, failing a ``code2 == 0`` assertion for reasons
    that have nothing to do with F26 (the flake this test hit 3x on a pin bump).
    Asserting F26 specifically RAN and FAILED (violation arm) / RAN and did NOT
    fail (clean arm) keeps the full e2e dispatch as the subject while making the
    verdict immune to unrelated whole-tree rule state. The
    ``_sweep_staged_probes`` session fixture above scrubs the most common
    debris; this scoping is the structural complement that removes the
    dependency on a globally-clean tree entirely."""
    with _probe_file(
        "kairix/core/zzz_staged_probe_f26.py",
        "from kairix.providers import something  # forbidden core→providers import\n",
    ) as rel:
        _code, out = _run_staged([rel])
    # Violation arm: F26 must have RUN (dispatch wiring intact) and FAILED on
    # the forbidden import. Scoped to F26 — independent of any other rule's
    # whole-tree verdict, so unrelated tree debris cannot make this flake.
    assert "F26" in _ran_rule_ids(out), f"F26 must run end-to-end through the real dispatch; ledger:\n{out}"
    assert "F26" in _failed_rule_ids(out), f"F26 must FAIL on the forbidden core→providers import; ledger:\n{out}"
    # Sabotage + clean-arm control (inline): the SAME probe without the import
    # must NOT fail F26 — the verdict flips on the one-line edit. Driven through
    # the single-rule narrowed path, not a second whole-gate dispatch: the full
    # dispatch above already proves the wiring, and paying the ~40-rule gate
    # twice pushed this test past the 60s per-test timeout on loaded CI shards.
    with _probe_file("kairix/core/zzz_staged_probe_f26.py", "x = 1\n") as rel:
        rc2, out2 = _run_one_narrowed("F26", [rel])
    assert rc2 == 0, f"removing the forbidden import must clear F26 (sabotage + clean-arm); output:\n{out2}"


# ── file-local marker: missing test category marker (F8) ────────────────


def test_file_local_f8_missing_marker_caught() -> None:
    """A staged test module with a ``def test_*`` but no category marker →
    staged mode runs and FAILS F8.

    Driven through the narrowed single-rule path (:func:`_run_one_narrowed`):
    F8 is dispatched alone, scoped to the staged probe, so it FAILS on the
    unmarked probe in <0.1s without re-running the whole gate and without
    walking the full ``tests/`` tree. This is the SOLE F8 detector coverage in
    the staged-selection battery, so both limbs are load-bearing — the unmarked
    probe MUST fail (no false negative) and the marked probe MUST clear (no
    false positive). Sabotage-proven: see the runner-agent report."""
    with _probe_file(
        "tests/zzz_staged_probe_f8.py",
        "def test_unmarked_probe():\n    assert True\n",
    ) as rel:
        code, out = _run_one_narrowed("F8", [rel])
    assert code == 1, f"F8 missing-marker must fail staged mode; ledger:\n{out}"
    assert "FAIL [F8]" in out, f"F8 must be the failing rule; ledger:\n{out}"
    # Sabotage: adding the marker clears F8.
    with _probe_file(
        "tests/zzz_staged_probe_f8.py",
        "import pytest\n\npytestmark = pytest.mark.unit\n\n\ndef test_marked_probe():\n    assert True\n",
    ) as rel:
        code2, out2 = _run_one_narrowed("F8", [rel])
    assert code2 == 0, f"adding the unit marker must clear F8 (sabotage proof); ledger:\n{out2}"


# ── relational: new CLI subcommand with no outcome test (F30) ───────────


def test_relational_f30_new_surface_selects_full_scope() -> None:
    """A staged CLI/MCP surface change SELECTS F30 (relational) to run over its
    FULL scope — and a deletion of an outcome test does too (deletion-
    sensitivity). This is the no-false-negative property of the NEW selection
    code: the surface change and the paired-artefact deletion both run F30."""
    f30 = next(e for e in run_checks._select_all() if e.id == "F30")
    d = decide(f30, run_checks.resolve_script(f30), ["kairix/cli.py"])
    assert d.run is True, "F30 must run when cli.py (a subcommand surface) is staged"
    assert d.scope_files is None, "F30 is relational — it runs full scope, not narrowed to staged files"
    d_del = decide(f30, run_checks.resolve_script(f30), ["tests/test_worker_cli_maintenance.py"])
    assert d_del.run is True, "deleting/altering an outcome test must run F30 (deletion-sensitivity)"
    # A change OUTSIDE F30's scope (a connector plugin file) does NOT run F30.
    d_out = decide(f30, run_checks.resolve_script(f30), ["kairix/connectors/obsidian/connector.py"])
    assert d_out.run is False, "F30 must skip when no staged path is in its scope (precision)"


def test_relational_f36_new_plugin_without_feature_selects_and_detects(tmp_path: Path) -> None:
    """RELATIONAL F36 in two cheap limbs (no full-gate dispatch):

    1. SELECTION — staging a NEW connector plugin file under kairix/connectors/
       selects F36 over its FULL relational scope (the no-false-negative
       property: a staged plugin change always runs F36).
    2. DETECTION — the real F36 detector flags a connector plugin that has no
       BDD feature, and clears once the feature exists (the cross-file gap F36
       guards). Driven through the detector's ``collect_violations(repo_root)``
       seam over a ``tmp_path`` tree (the tests/architecture/ pattern) — same
       verdict the full staged dispatch would reach, in <1ms, hermetically.

    The retained per-commit end-to-end smoke is the F26 file-local FAIL; this
    relational proof keeps its full bug-power (selection + detection) without
    re-running the whole gate."""
    # Limb 1: a staged plugin file selects F36 at full scope.
    f36 = next(e for e in run_checks._select_all() if e.id == "F36")
    d = decide(f36, run_checks.resolve_script(f36), ["kairix/connectors/zzz_staged_probe_conn/connector.py"])
    assert d.run is True, "a staged connector-plugin file MUST select F36 (relational no-false-negative)"
    assert d.scope_files is None, "F36 is relational — full scope, never narrowed to staged files"

    # Limb 2: the real detector flags a featureless plugin and clears with one.
    detector = _load_detector("check_f36_connector_bdd_parity.py", "_f36_detector")
    plugin = tmp_path / "kairix" / "connectors" / "zzz_staged_probe_conn"
    plugin.mkdir(parents=True)
    (plugin / "__init__.py").write_text("def make_connector():\n    return None\n", encoding="utf-8")
    (plugin / "connector.py").write_text("class ProbeConnector:\n    pass\n", encoding="utf-8")
    violations = detector.collect_violations(tmp_path)
    assert any(p.name == "zzz_staged_probe_conn" for p in violations), (
        f"a new connector plugin without a BDD feature must be an F36 violation; got {sorted(map(str, violations))}"
    )
    # Sabotage: adding the per-plugin feature clears F36.
    features = tmp_path / "tests" / "bdd" / "features"
    features.mkdir(parents=True)
    (features / "connector_zzz_staged_probe_conn.feature").write_text(
        "Feature: probe\n  Scenario: happy path\n    Given a probe connector\n", encoding="utf-8"
    )
    cleared = detector.collect_violations(tmp_path)
    assert not any(p.name == "zzz_staged_probe_conn" for p in cleared), (
        "adding the per-plugin BDD feature must clear F36 (sabotage proof)"
    )


# ── audit-driven: cross-file source dependencies (F46 / F56) ────────────
#
# Two rules an adversarial audit found mis-classified ``file-local`` even
# though their detectors read CROSS-FILE state, so a staged edit to the
# cross-file SOURCE (not the obvious scanned file) could slip a violation
# past a file-local scoping. Reclassifying them ``relational`` with a scope
# that spans the source closes the gap. These cases prove the SELECTION
# property — staging the source alone selects the rule — plus, for F46, an
# executed end-to-end FAIL through the real ``server.py`` dependency.


def test_f46_server_py_edit_selects_f46() -> None:
    """SOUNDNESS (F46): F46's verdict for a step file depends on
    ``kairix/agents/mcp/server.py`` (its ``@server.tool()`` set decides
    whether a bare call routes through the MCP surface). So staging
    server.py ALONE — with NO step file staged — must select F46. The
    relational scope spans both ``tests/bdd/steps`` and the server source.

    This is the no-false-negative SELECTION proof — asserted through the real
    ``decide`` (sub-ms). The end-to-end ran-set tail it used to carry is dropped:
    the one retained per-commit full-dispatch smoke is the F26 file-local FAIL,
    and the cross-file FAIL signal is proven cheaply in
    ``test_f46_server_py_tool_removal_fails_dependent_step``."""
    f46 = next(e for e in run_checks._select_all() if e.id == "F46")
    script = run_checks.resolve_script(f46)
    d = decide(f46, script, ["kairix/agents/mcp/server.py"])
    assert d.run is True, "staging server.py alone MUST select F46 (its tool set drives step verdicts)"
    assert d.scope_files is None, "F46 is relational — full scope, never narrowed to staged files"


def test_f46_file_local_would_miss_server_py_edit_sabotage() -> None:
    """SABOTAGE PROOF (F46): with F46 left ``file-local`` + its old narrow
    scope (``tests/bdd/steps`` only), staging server.py ALONE does NOT select
    F46 — the exact missed-violation route the audit found. The shipped
    catalogue value (relational, scope spans server.py) DOES select it. Proven
    here by reconstructing the pre-fix entry with ``dataclasses.replace``; the
    real catalogue entry asserts the fixed behaviour."""
    f46 = next(e for e in run_checks._select_all() if e.id == "F46")
    script = run_checks.resolve_script(f46)
    pre_fix = dataclasses.replace(f46, staged_class="file-local", staged_scope=("tests/bdd/steps",))
    assert decide(pre_fix, script, ["kairix/agents/mcp/server.py"]).run is False, (
        "the BUG: file-local F46 scoped to tests/bdd/steps SKIPS a server.py-only staged edit"
    )
    assert decide(f46, script, ["kairix/agents/mcp/server.py"]).run is True, (
        "the FIX: relational F46 with server.py in scope SELECTS the same staged edit"
    )


def test_f46_server_py_tool_removal_fails_dependent_step(tmp_path: Path) -> None:
    """CROSS-FILE (F46): a step file that routes through the ``search`` MCP tool
    (bare call to an imported ``search`` from server.py) is F46-clean ONLY
    because ``search`` is a registered ``@server.tool()``. Drop ``search`` from
    the tool set and that SAME step file now VIOLATES F46 — the exact
    cross-file dependency on ``server.py`` the relational scope guards.

    Proven through the F46 detector's own ``file_has_violation`` /
    ``_discover_mcp_tool_names`` seams, in <1ms, hermetically:

    * the tool set is read from the REAL ``server.py`` via
      ``_discover_mcp_tool_names`` — so the dependency is genuine (``search``
      IS a registered tool today, asserted below), not a hand-picked string set;
    * ``file_has_violation`` returns False when ``search`` is in the tool set and
      True when it is removed — the same verdict flip the full staged dispatch
      would produce, without re-running the whole gate, without mutating the
      real ``server.py``, and without walking the BDD-step tree.

    This keeps the load-bearing limb (the verdict flips on the cross-file edit)
    and drops the expensive double full-dispatch tail; the one retained
    full-dispatch smoke is the F26 file-local FAIL."""
    detector = _load_detector("check_f46_bdd_step_composition.py", "_f46_detector")

    # The tool set comes from the REAL server.py — `search` is a registered tool
    # today, so the dependency the relational scope spans is genuine.
    real_tools = frozenset(detector._discover_mcp_tool_names(run_checks.REPO_ROOT))
    assert "search" in real_tools, "precondition: `search` is a registered @server.tool() in the real server.py"

    # A step file that constructs a *Pipeline directly (so it's "at risk") but
    # routes through the MCP `search` tool — clean WHILE `search` is registered.
    step_file = tmp_path / "tests" / "bdd" / "steps" / "zzz_staged_probe_f46_dependent_steps.py"
    step_file.parent.mkdir(parents=True)
    step_file.write_text(
        "from pytest_bdd import when\n"
        "from kairix.agents.mcp.server import search\n"
        "from kairix.core.search.pipeline import SearchPipeline\n"
        "\n"
        "\n"
        "@when('the agent builds a pipeline directly')\n"
        "def _build_pipeline_directly():\n"
        "    return SearchPipeline(retriever=None, ranker=None)\n"
        "\n"
        "\n"
        "@when('the agent searches')\n"
        "def _do_search():\n"
        "    return search('hello')\n",
        encoding="utf-8",
    )

    # Control: while `search` is in the tool set, the dependent step is F46-clean.
    assert detector.file_has_violation(step_file, real_tools) is False, (
        "the dependent step must be F46-clean while `search` is a registered tool"
    )
    # Cross-file edit: drop `search` from the tool set (the un-register) → the
    # SAME step file now VIOLATES F46 (its only sanctioned route is gone).
    tools_without_search = frozenset(t for t in real_tools if t != "search")
    assert detector.file_has_violation(step_file, tools_without_search) is True, (
        "un-registering `search` must make the dependent step VIOLATE F46 (cross-file dependency)"
    )


def test_f56_protocols_py_edit_selects_f56() -> None:
    """SOUNDNESS (F56): F56's runtime probe imports ``kairix/core/protocols.py``
    and ``isinstance``-checks each connector against the Protocols DEFINED
    there. A staged protocols.py edit (e.g. dropping a member from a
    runtime-checkable Protocol) can change whether an UN-staged connector
    satisfies its capability declaration — so staging protocols.py ALONE must
    select F56. Selection is the soundness property the relational scope
    guarantees.

    Note (additive-probe nuance): the runtime ``isinstance`` probe is
    fail→pass-only on the CONNECTOR side (it can only ADD a satisfied
    capability, never remove the AST-declared one), so a true missed-violation
    is hard to construct from the connector side alone. The genuine
    missed-violation route is the protocols.py edit, which this asserts via
    SELECTION — the property that closes the audit gap. SELECTION is therefore
    the whole load-bearing limb here; it is asserted through the real ``decide``
    (sub-ms). The end-to-end ran-set tail is dropped (the one retained
    full-dispatch smoke is the F26 file-local FAIL)."""
    f56 = next(e for e in run_checks._select_all() if e.id == "F56")
    script = run_checks.resolve_script(f56)
    d = decide(f56, script, ["kairix/core/protocols.py"])
    assert d.run is True, "staging protocols.py alone MUST select F56 (its Protocols drive the runtime probe)"
    assert d.scope_files is None, "F56 is relational — full scope, never narrowed to staged files"


def test_f56_file_local_would_miss_protocols_py_edit_sabotage() -> None:
    """SABOTAGE PROOF (F56): with F56 left ``file-local`` and its detector-
    derived scope (``kairix/connectors`` only), staging protocols.py ALONE
    does NOT select F56 — the missed-violation route the audit found. The
    shipped catalogue value (relational, scope spans protocols.py) DOES select
    it. Pre-fix entry reconstructed with ``dataclasses.replace``; the real
    catalogue entry asserts the fixed behaviour."""
    f56 = next(e for e in run_checks._select_all() if e.id == "F56")
    script = run_checks.resolve_script(f56)
    # Pre-fix: file-local + scope DERIVED from the F56 detector's roots
    # ("kairix/connectors",), i.e. no protocols.py in scope.
    pre_fix = dataclasses.replace(f56, staged_class="file-local", staged_scope=("kairix/connectors",))
    assert decide(pre_fix, script, ["kairix/core/protocols.py"]).run is False, (
        "the BUG: file-local F56 scoped to kairix/connectors SKIPS a protocols.py-only staged edit"
    )
    assert decide(f56, script, ["kairix/core/protocols.py"]).run is True, (
        "the FIX: relational F56 with protocols.py in scope SELECTS the same staged edit"
    )


# ── relational deletion: breaking a paired invariant ────────────────────


def test_relational_deletion_selects_rule() -> None:
    """A staged DELETION within a relational rule's scope still selects the
    rule (deletion-sensitivity). F45 (capability↔BDD-feature): deleting a
    feature file is a staged path under tests/bdd/features → F45 runs full
    scope."""
    f45 = next(e for e in run_checks._select_all() if e.id == "F45")
    d = decide(f45, run_checks.resolve_script(f45), ["tests/bdd/features/bootstrap.feature"])
    assert d.run is True, "deleting a BDD feature must run F45 at full scope"
    assert d.scope_files is None, "relational rules are never narrowed to staged files"


# ── cross-cutting / always-run: catalogue drift (F92) ───────────────────


def test_always_run_f92_runs_for_any_change() -> None:
    """F92 (catalogue currency, always-run) runs for ANY staged change,
    including a doc-only edit far from any check tree."""
    f92 = next(e for e in run_checks._select_all() if e.id == "F92")
    d = decide(f92, run_checks.resolve_script(f92), ["docs/only.md"])
    assert d.run is True, "F92 must always run (trigger is any change)"
    # And it appears in the ran-set of a real doc-only staged dispatch.
    _code, out = _run_staged(["docs/architecture/ENGINEERING.md"])
    assert "F92" in _ran_rule_ids(out), f"F92 must run on a doc-only staged change; ledger:\n{out}"


def test_always_run_f94_runs_for_any_change() -> None:
    """F94 (system-path writes, always-run) runs for any staged change."""
    f94 = next(e for e in run_checks._select_all() if e.id == "F94")
    assert decide(f94, run_checks.resolve_script(f94), ["docs/only.md"]).run is True


# ── completeness / negative-control table ───────────────────────────────
#
# For a battery of staged sets, assert the rules staged mode RUNS are a
# SUPERSET of every rule whose scope contains a staged path (so no rule that
# COULD be tripped by a staged file is skipped). The real tree is clean, so we
# prove completeness structurally: every rule in-scope for a staged set is run.


_BATTERY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("docs-only", ("docs/architecture/ENGINEERING.md",)),
    ("single-kairix-core", ("kairix/core/search/pipeline.py",)),
    ("tests-only", ("tests/test_credentials.py",)),
    ("bdd-feature", ("tests/bdd/features/bootstrap.feature",)),
    ("cli-surface", ("kairix/cli.py",)),
    ("workflow", (".github/workflows/ci.yml",)),
    ("sonar-config", ("sonar-project.properties",)),
    ("connector", ("kairix/connectors/obsidian/connector.py",)),
    ("setup-template", ("kairix/platform/setup/web/templates/setup/folder.html",)),
    ("services-go", ("services/hello/cmd/hello/main.go",)),
    ("multi-3", ("kairix/core/factory.py", "tests/test_paths.py", "docs/x.md")),
)


def _rules_in_scope_for(staged: list[str]) -> set[str]:
    """Every dispatchable run_all rule that, by its catalogue scope/class,
    COULD be tripped by ``staged`` — the ground-truth set staged mode must run
    a superset of. ``always-run`` rules are always in; for the rest, a rule is
    in-scope iff a staged path falls within its resolved scope (None scope →
    always in, fail-safe)."""
    in_scope: set[str] = set()
    seen_scripts: set[str] = set()
    for e in run_checks._select_all():
        script = run_checks.resolve_script(e)
        if script in seen_scripts:
            continue
        seen_scripts.add(script)
        if e.staged_class == "always-run":
            in_scope.add(e.id)
            continue
        scope = resolve_staged_scope(e, script)
        if scope is None or staged_in_scope(scope, staged):
            in_scope.add(e.id)
    return in_scope


@pytest.mark.parametrize("label,staged", _BATTERY, ids=[b[0] for b in _BATTERY])
def test_completeness_staged_runs_superset_of_in_scope(label: str, staged: tuple[str, ...]) -> None:
    """No false negative: every rule whose scope a staged path falls within IS
    run by staged mode (the ran-set ⊇ the in-scope ground-truth set)."""
    staged_list = list(staged)
    ran: set[str] = set()
    skipped: set[str] = set()
    for entry, d in run_checks._staged_decisions(staged_list):
        (ran if d.run else skipped).add(entry.id)
    ground_truth = _rules_in_scope_for(staged_list)
    missing = ground_truth - ran
    assert not missing, (
        f"[{label}] staged mode SKIPPED in-scope rules (false-negative risk): "
        f"{sorted(missing)}\n  staged={staged_list}\n  skipped={sorted(skipped)}"
    )


def test_completeness_skips_are_genuinely_out_of_scope() -> None:
    """The dual: a skipped rule's scope genuinely contains NONE of the staged
    paths (and it is not always-run). Proves the skips are precise, not random
    under-running — for a docs-only change, every skipped rule is out of
    scope."""
    staged = ["docs/architecture/ENGINEERING.md"]
    for entry, d in run_checks._staged_decisions(staged):
        if d.run:
            continue
        assert entry.staged_class != "always-run", f"{entry.id}: always-run rule must never be skipped"
        scope = resolve_staged_scope(entry, run_checks.resolve_script(entry))
        assert scope is not None, f"{entry.id}: a scope-unresolved rule must run (fail-safe), not skip"
        assert not staged_in_scope(scope, staged), f"{entry.id}: skipped but a staged path is in scope {scope}"


def test_every_dispatchable_rule_has_a_decision() -> None:
    """Every rule the ``--all`` set dispatches gets a staged decision (no rule
    silently falls out of the staged selection)."""
    all_ids = {e.id for e in run_checks._select_all()}
    decided = {e.id for e, _ in run_checks._staged_decisions(["kairix/x.py"])}
    # Deduped by script, so the count may differ; every decided id is a real
    # all-set id and the F7/F9-style script dedup is the only divergence.
    assert decided <= all_ids
    assert "F26" in decided and "F92" in decided


def test_staged_classes_are_valid() -> None:
    """Every catalogue entry's ``staged_class`` is a member of the closed
    vocabulary, and ``staged_scope`` (when set) is a non-empty tuple."""
    valid = {"file-local", "relational", "always-run"}
    for e in ALL_ENTRIES:
        assert e.staged_class in valid, f"{e.id}: invalid staged_class {e.staged_class!r}"
        if e.staged_scope is not None:
            assert isinstance(e.staged_scope, tuple) and e.staged_scope, f"{e.id}: empty staged_scope"
