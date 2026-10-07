"""Tests for the diff-scoped mutation runner (#499 Phase 1).

Covers ``scripts/checks/mutation_parity.py`` — the mechanical sabotage
control. The runner is itself a correctness gate, so it carries the same
discipline it enforces: every branch is driven through the module's public
functions, the survivor/killed verdict is proven both ways, and the absence of any
survivor list (PLA-472 — any survivor fails) is pinned.

Sabotage proofs (executed; mutate prod -> confirm fail -> restore):

  * generate_mutants: change ``_COMPARE_SWAPS[ast.Gt]`` from ``(">", ">=")``
    to ``(">", ">")`` -> ``test_generate_mutants_swaps_comparisons`` fails
    (the produced mutation no longer differs); restore -> green.
  * _verdict: change ``if not survivors`` to ``if len(survivors) < 2``
    -> ``test_no_survivor_list_can_excuse_a_survivor`` fails (a single
    survivor would pass); restore -> green.
  * changed_lines: change the hunk-count guard ``if count > 0`` to
    ``if count >= 0`` -> ``test_changed_lines_ignores_pure_deletions``
    fails (a 0-line deletion hunk would add a phantom line); restore.

The full tautology->survivor->strengthen->killed end-to-end proof against
the real ``_run_impacted_tests`` subprocess path is captured in the agent
report (it stages a throwaway target + tautological test and runs the CLI).
This suite stays subprocess-free so it lives in the fast unit tier.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import mutation_parity as mp  # noqa: E402

pytestmark = pytest.mark.unit


# ── diff parsing -> changed lines ───────────────────────────────────────


def test_changed_lines_parses_added_hunk() -> None:
    """A unified-0 diff for a kairix/*.py file yields its added line numbers."""
    diff = (
        "diff --git a/kairix/x.py b/kairix/x.py\n"
        "--- a/kairix/x.py\n"
        "+++ b/kairix/x.py\n"
        "@@ -10,0 +11,2 @@\n"
        "+    a = 1\n"
        "+    b = 2\n"
    )
    result = mp._changed_lines_from_diff(diff)
    assert result == {Path("kairix/x.py"): {11, 12}}


def test_changed_lines_single_line_hunk_defaults_count_to_one() -> None:
    """A hunk header with no explicit +count means a single changed line."""
    diff = "+++ b/kairix/y.py\n@@ -5 +5 @@\n+    z = 3\n"
    assert mp._changed_lines_from_diff(diff) == {Path("kairix/y.py"): {5}}


def test_changed_lines_ignores_non_kairix_and_non_py() -> None:
    """Tests, scripts, and non-python files are out of mutation scope."""
    diff = (
        "+++ b/tests/test_x.py\n@@ -1 +1 @@\n+changed\n"
        "+++ b/kairix/notes.md\n@@ -1 +1 @@\n+changed\n"
        "+++ b/scripts/thing.py\n@@ -1 +1 @@\n+changed\n"
    )
    assert mp._changed_lines_from_diff(diff) == {}


def test_changed_lines_ignores_pure_deletions() -> None:
    """A hunk that only deletes (``+count`` == 0) adds no phantom line."""
    diff = "+++ b/kairix/d.py\n@@ -5,3 +4,0 @@\n-old\n-old\n-old\n"
    assert mp._changed_lines_from_diff(diff) == {}


# ── function-span narrowing ─────────────────────────────────────────────


def test_enclosing_function_lines_keeps_body_lines_only() -> None:
    """Only changed lines INSIDE a def body survive the narrowing."""
    source = "X = 1\n\ndef f(a):\n    return a > 0\n"
    # line 1 (module const) is outside any def; line 4 (return) is in f's body.
    assert mp._enclosing_function_lines(source, {1, 4}) == {4}


def test_enclosing_function_lines_handles_syntax_error() -> None:
    """Unparseable source narrows to the empty set rather than raising."""
    assert mp._enclosing_function_lines("def (:\n", {1}) == set()


# ── mutant generation ───────────────────────────────────────────────────


def _mutations(source: str, scope: set[int]) -> set[tuple[str, str]]:
    return {(m.original, m.mutation) for m in mp.generate_mutants(source, Path("kairix/t.py"), scope)}


def test_generate_mutants_swaps_comparisons() -> None:
    """Each comparison operator is swapped to its mutation partner."""
    source = "def f(a, b):\n    return a > b\n"
    muts = _mutations(source, {2})
    assert (">", ">=") in muts
    # The mutation must DIFFER from the original (the sabotage anchor).
    assert all(orig != mut for orig, mut in muts)


def test_generate_mutants_swaps_boolean_operators() -> None:
    """``and`` becomes ``or``."""
    source = "def f(a, b):\n    return a and b\n"
    assert ("and", "or") in _mutations(source, {2})


def test_generate_mutants_flips_bool_literal() -> None:
    """``True`` becomes ``False``."""
    source = "def f():\n    return True\n"
    assert ("True", "False") in _mutations(source, {2})


def test_generate_mutants_respects_scope() -> None:
    """A comparison on a line NOT in scope yields no mutant."""
    source = "def f(a, b):\n    return a > b\n"
    assert _mutations(source, {99}) == set()


def test_generate_mutants_empty_on_syntax_error() -> None:
    assert mp.generate_mutants("def (:\n", Path("kairix/t.py"), {1}) == []


def test_generated_mutant_source_is_a_one_token_delta() -> None:
    """The mutated source differs from the original by exactly the swap."""
    source = "def f(a, b):\n    return a == b\n"
    [mutant] = [m for m in mp.generate_mutants(source, Path("kairix/t.py"), {2}) if m.original == "=="]
    assert "a != b" in mutant.mutated_source
    assert "a == b" not in mutant.mutated_source


# ── verdict translation ─────────────────────────────────────────────────


def _result(key_path: str, lineno: int, original: str, mutation: str) -> mp.MutantResult:
    mutant = mp.Mutant(
        path=Path(key_path),
        lineno=lineno,
        col=0,
        original=original,
        mutation=mutation,
        mutated_source="",
    )
    return mp.MutantResult(mutant=mutant, survived=True, detail="survived", elapsed_s=0.1)


def test_verdict_clean_when_no_survivors() -> None:
    assert mp._verdict([]) == 0


def test_verdict_fails_on_any_survivor(capsys: pytest.CaptureFixture[str]) -> None:
    """Any survivor fails and every survivor is reported — no survivor list
    can excuse one (PLA-472 retired the ratchet)."""
    survivors = [_result("kairix/a.py", 1, ">", ">="), _result("kairix/b.py", 9, "==", "!=")]
    assert mp._verdict(survivors) == 1
    err = capsys.readouterr().err
    assert "2 survivor(s)" in err
    assert "kairix/a.py:1" in err
    assert "kairix/b.py:9" in err


def test_no_survivor_list_can_excuse_a_survivor(tmp_path: Path) -> None:
    """PLA-472: the runner has no survivor-list channel. The old
    ``--baseline`` / ``--write-baseline`` flags are rejected by the CLI
    (argparse exit 2, before any diff is read), the verdict takes only the
    survivor list, and the module no longer loads or writes a survivors file —
    so a re-created ``mutation-survivors-files.txt`` naming the exact
    survivor cannot rescue it.

    Sabotage proof (executed): re-adding a ``--baseline`` argument to
    ``main``'s parser flips the SystemExit assertion red (argparse accepts
    it and the run proceeds); re-adding a module-level ``_load_baseline``
    flips the hasattr assertion red. Restored -> green.
    """
    survivors_file = tmp_path / ".architecture" / "baseline" / "mutation-survivors-files.txt"
    survivors_file.parent.mkdir(parents=True)
    survivors_file.write_text("kairix/a.py:1:>->>=\n", encoding="utf-8")

    for flag in (["--baseline", str(survivors_file)], ["--write-baseline"]):
        with pytest.raises(SystemExit) as exc:
            mp.main(flag)
        assert exc.value.code == 2

    assert mp._verdict([_result("kairix/a.py", 1, ">", ">=")]) == 1
    assert not hasattr(mp, "_load_baseline")
    assert not hasattr(mp, "_ratchet")


# ── impacted-test selection + report shape ──────────────────────────────


def test_module_path_maps_to_dotted_import() -> None:
    assert mp._module_path(Path("kairix/core/factory.py")) == "kairix.core.factory"


def test_same_module_tests_picks_a_modules_own_test_files() -> None:
    """``test_<mod>.py`` counts; an unrelated file that merely contains the
    module name (``test_cli_mcp_parity_recommend.py`` — does NOT start with
    ``test_recommend_``) does not."""
    paths = {Path("kairix/use_cases/recommend.py"), Path("kairix/core/factory.py")}
    found = {
        "tests/use_cases/test_recommend.py",  # recommend.py's own test
        "tests/contracts/test_cli_mcp_parity_recommend.py",  # NOT test_recommend / test_recommend_*
        "tests/core/test_factory.py",  # factory.py's own test
        "tests/integration/test_pipeline_cache_race.py",  # incidental importer
    }
    assert mp._same_module_tests(paths, found) == [
        "tests/core/test_factory.py",
        "tests/use_cases/test_recommend.py",
    ]


def test_same_module_tests_matches_aspect_suffixed_files() -> None:
    """A module's aspect-suffixed unit tests (``test_<mod>_<aspect>.py``) count
    as same-module, so an aspect-named killer (e.g. test_topology_config_parser
    for topology.py) is not evicted from the impacted-test window.

    Sabotage proof: revert ``_same_module_tests`` to the exact ``test_<mod>.py``
    set and these aspect-suffixed files fall out — this assertion fails.
    """
    paths = {
        Path("kairix/config/topology.py"),
        Path("kairix/core/connectors/topology_applier.py"),
    }
    found = {
        "tests/unit/test_topology_config_parser.py",  # topology.py aspect test
        "tests/unit/test_topology_applier_unit.py",  # applier aspect test
        "tests/integration/test_unrelated_importer.py",  # incidental importer
    }
    same = mp._same_module_tests(paths, found)
    assert "tests/unit/test_topology_config_parser.py" in same
    assert "tests/unit/test_topology_applier_unit.py" in same
    assert "tests/integration/test_unrelated_importer.py" not in same


def test_prioritise_keeps_same_module_test_even_when_cap_would_evict_it() -> None:
    """The fix: a module's own test survives the cap even when a co-mutated,
    widely-imported file floods ``found`` with alphabetically-earlier importers.

    Sabotage proof: revert ``_prioritise`` to ``sorted(found)[:MAX]`` and the
    own-test (which sorts after the 'aaa' crowd) drops out of the window — this
    assertion fails. The crowd models the importers of a co-mutated factory.py.
    """
    own = "tests/use_cases/test_recommend.py"
    crowd = {f"tests/aaa/test_{i:03d}.py" for i in range(mp.MAX_IMPACTED_TEST_FILES + 10)}
    found = crowd | {own}
    paths = {Path("kairix/use_cases/recommend.py"), Path("kairix/core/factory.py")}

    result = mp._prioritise(found, paths)

    assert own in result  # the mutated module's own test is never evicted
    assert result[0] == own  # same-module tests come first
    assert len(result) <= mp.MAX_IMPACTED_TEST_FILES  # the long tail stays capped


def test_survivor_report_carries_f21_action_markers() -> None:
    """The F21 affordance contract: fix:/next:/run: markers present."""
    report = mp._survivor_report(_result("kairix/a.py", 7, ">", ">="))
    assert "fix:" in report
    assert "next:" in report
    assert "run:" in report
    assert "mutant survived" in report
