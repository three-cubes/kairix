"""Outcome tests for the fast-tier reranker guard (``tests/fixtures/reranker_guard.py``, #493).

The guard is a pytest ``pytest_runtest_call`` wrapper, so it is proven the way
a user meets it: the guard's exact source is installed as the ``conftest.py``
of a throwaway pytest run under ``tmp_path``, a small inner test module drives
each case, and the inner run's JUnit XML is read back. The inner tests stand in
for the real ``sentence_transformers`` package by putting a ``__file__``-carrying
module object in ``sys.modules`` (removed in teardown) — no kairix internals are
patched and no model is loaded.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_GUARD_SOURCE = Path(__file__).parent / "fixtures" / "reranker_guard.py"
_GUARD_PREFIX = "Real cross-encoder reranker load found in fast-tier test"

_INNER_TESTS = '''
import sys
import types

import pytest


@pytest.fixture
def real_module_loaded():
    """Simulate the production cross-encoder import during the test call."""

    def _load():
        module = types.ModuleType("sentence_transformers")
        module.__file__ = "/site-packages/sentence_transformers/__init__.py"
        sys.modules["sentence_transformers"] = module

    yield _load
    sys.modules.pop("sentence_transformers", None)


@pytest.mark.unit
def test_loads_and_passes(real_module_loaded):
    real_module_loaded()


@pytest.mark.unit
def test_loads_and_raises(real_module_loaded):
    real_module_loaded()
    raise ValueError("original-boom from the test call")


@pytest.mark.unit
def test_raises_without_loading():
    raise ValueError("unrelated-boom")


@pytest.mark.unit
def test_clean_call():
    assert True
'''


@pytest.fixture(scope="module")
def inner_outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[str, str]]:
    """Run the inner suite once under the guard; map test name -> (outcome, message)."""
    root = tmp_path_factory.mktemp("reranker-guard")
    (root / "conftest.py").write_text(_GUARD_SOURCE.read_text())
    (root / "pytest.ini").write_text("[pytest]\nmarkers =\n    unit: fast tier\n")
    (root / "test_inner.py").write_text(_INNER_TESTS)
    junit = root / "junit.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(root / "test_inner.py"),
            "-p",
            "no:cacheprovider",
            "-q",
            f"--junitxml={junit}",
            f"--rootdir={root}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    outcomes: dict[str, tuple[str, str]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            outcomes[case.attrib["name"]] = ("passed", "")
        else:
            outcomes[case.attrib["name"]] = ("failed", f"{failure.attrib.get('message', '')}\n{failure.text or ''}")
    return outcomes


def test_passing_call_that_loads_the_real_module_is_attributed(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    outcome, message = inner_outcomes["test_loads_and_passes"]
    assert outcome == "failed"
    assert _GUARD_PREFIX in message
    assert "test_loads_and_passes" in message


def test_raising_call_that_loads_the_real_module_is_attributed_and_keeps_its_error(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    """A raising call (e.g. a timeout mid-load) is still attributed; its own error stays visible."""
    outcome, message = inner_outcomes["test_loads_and_raises"]
    assert outcome == "failed"
    assert _GUARD_PREFIX in message, f"guard did not attribute the raising loader; got:\n{message}"
    assert "ValueError: original-boom from the test call" in message


def test_raising_call_without_loading_propagates_its_error_unchanged(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    outcome, message = inner_outcomes["test_raises_without_loading"]
    assert outcome == "failed"
    assert "unrelated-boom" in message
    assert _GUARD_PREFIX not in message


def test_call_that_does_not_load_the_real_module_passes(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    assert inner_outcomes["test_clean_call"] == ("passed", "")
