"""Unit tests for F46 (``scripts/checks/check_f46_bdd_step_composition.py``).

F46 forbids step implementations under ``tests/bdd/steps/*.py`` from
constructing ``*Pipeline`` classes directly when none of their
``@given/@when/@then/@step`` decorated functions reach a sanctioned
entry point (CLI main / MCP tool / ``kairix.core.factory.build_*``)
within depth ≤ 2 of their call graph.

Each test has an inline sabotage-proof: introduce a violation, confirm
the detector flags it; remove the violation, confirm the detector
clears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f46_bdd_step_composition.py"


def _load_detector():
    """Load the F46 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f46_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f46_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_step_constructs_pipeline_directly_is_flagged(tmp_path: Path) -> None:
    """A step file that constructs ``SearchPipeline(...)`` directly,
    with no step that reaches a sanctioned entry point, is flagged.

    Sabotage-proof inline: replacing the direct construction with a
    factory call clears the flag.
    """
    detector = _load_detector()
    step_path = tmp_path / "tests" / "bdd" / "steps" / "f46_violator_steps.py"
    _write(
        step_path,
        """\
from pytest_bdd import when

from kairix.core.search.pipeline import SearchPipeline


@when("I run a search")
def run_search() -> None:
    pipe = SearchPipeline()
    pipe.search("q")
""",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("tests/bdd/steps/f46_violator_steps.py") in violations

    # Sabotage: replace direct construction with a factory call.
    _write(
        step_path,
        """\
from pytest_bdd import when

from kairix.core import factory


@when("I run a search")
def run_search() -> None:
    pipe = factory.build_search_pipeline()
    pipe.search("q")
""",
    )
    assert detector.collect_violations(tmp_path) == set()


def test_step_routes_through_factory_passes(tmp_path: Path) -> None:
    """A step file that calls ``factory.build_search_pipeline`` from
    within a step body is not flagged, even if a ``*Pipeline`` class
    name appears in a type annotation or comment.

    Sabotage-proof inline: removing the factory call and constructing
    the pipeline directly causes the detector to flag the file.
    """
    detector = _load_detector()
    step_path = tmp_path / "tests" / "bdd" / "steps" / "f46_compliant_steps.py"
    # SearchPipeline appears as a Call here too — but the step ALSO
    # reaches the factory, so the file is compliant.
    _write(
        step_path,
        """\
from pytest_bdd import when

from kairix.core import factory
from kairix.core.search.pipeline import SearchPipeline


@when("I run a search")
def run_search() -> None:
    pipe = factory.build_search_pipeline()
    # SearchPipeline name appears below just to keep _PIPELINE in scope;
    # the sanctioned factory call is what makes this file compliant.
    _ = SearchPipeline()
    pipe.search("q")
""",
    )
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: remove the factory call. Now the file only constructs
    # the pipeline directly with no sanctioned entry point reached —
    # detector flags it.
    _write(
        step_path,
        """\
from pytest_bdd import when

from kairix.core.search.pipeline import SearchPipeline


@when("I run a search")
def run_search() -> None:
    pipe = SearchPipeline()
    pipe.search("q")
""",
    )
    violations = detector.collect_violations(tmp_path)
    assert Path("tests/bdd/steps/f46_compliant_steps.py") in violations


def test_real_repo_gate_is_green() -> None:
    """The real F46 detector run against the full repo emits no
    violations (there is no grandfathering).
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F46's REMEDIATION text must satisfy F21 — the agent reading a
    failure must get the correction action inline.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
