"""Outcome tests for the runtime F1 / F2 guard (``tests/fixtures/process_state_guard.py``).

The guard is a pytest plugin, so it is proven the way a user meets it: its
exact source is installed as the ``conftest.py`` of a throwaway pytest run
under ``tmp_path``, with the guarded names overridden to a fake top-level
package ``fakepkg`` and env prefix ``FAKEPKG_`` (so the proof needs no real
kairix and touches no real ``KAIRIX_*`` variable). Small inner test modules
drive each rule, and the inner run's JUnit XML is read back.
"""

from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_GUARD_SOURCE = Path(__file__).parent / "fixtures" / "process_state_guard.py"

# Appended to the guard source in the throwaway conftest: point the guard at
# the fake package / prefix and add a session baseline like tests/conftest.py's.
_CONFTEST_OVERRIDES = """

import os as _os

GUARDED_PACKAGES = ("fakepkg",)
GUARDED_ENV_PREFIXES = ("FAKEPKG_",)


@pytest.fixture(scope="session", autouse=True)
def _baseline():
    with allow_baseline_writes():
        _os.environ["FAKEPKG_BASELINE"] = "1"
    yield
    with allow_baseline_writes():
        del _os.environ["FAKEPKG_BASELINE"]
"""

_FAKEPKG_INIT = """
VALUE = 1


def helper():
    return 1


class Thing:
    x = 1
"""

_INNER_TESTS = """
import importlib
import os
import sys
import types
from unittest import mock

import pytest

import fakepkg
import fakepkg.sub


# --- F2: env writes -----------------------------------------------------------


def test_env_baseline_is_exempt_and_reads_are_clean():
    assert os.environ["FAKEPKG_BASELINE"] == "1"
    assert os.environ.get("FAKEPKG_MISSING") is None


def test_env_subscript_write():
    os.environ["FAKEPKG_X"] = "1"
    del os.environ["FAKEPKG_X"]


def test_env_patch_dict():
    with mock.patch.dict(os.environ, {"FAKEPKG_X": "1"}):
        pass


def test_env_monkeypatch_setenv(monkeypatch):
    monkeypatch.setenv("FAKEPKG_X", "1")


def test_env_through_os_dunder_dict():
    os.__dict__["environ"].update({"FAKEPKG_X": "1"})
    vars(os)["environ"].pop("FAKEPKG_X")


def test_env_putenv_unsetenv():
    os.putenv("FAKEPKG_X", "1")
    os.unsetenv("FAKEPKG_X")


@pytest.fixture
def env_writing_fixture():
    os.environ["FAKEPKG_FROM_FIXTURE"] = "1"
    yield
    os.environ.pop("FAKEPKG_FROM_FIXTURE", None)


def test_env_write_in_fixture_setup(env_writing_fixture):
    pass


def test_env_other_keys_are_clean(monkeypatch):
    monkeypatch.setenv("OTHER_X", "1")
    os.environ["OTHER_Y"] = "1"
    del os.environ["OTHER_Y"]


# --- F1: sys.modules swaps ----------------------------------------------------


@pytest.fixture
def swapped_submodule():
    original = sys.modules["fakepkg.sub"]
    sys.modules["fakepkg.sub"] = types.ModuleType("fakepkg.sub")
    yield
    sys.modules["fakepkg.sub"] = original


def test_sys_modules_swap(swapped_submodule):
    pass


def test_sys_modules_patch_dict():
    with mock.patch.dict(sys.modules, {"fakepkg.sub": types.ModuleType("fakepkg.sub")}):
        pass


def test_sys_modules_monkeypatch_setitem(monkeypatch):
    monkeypatch.setitem(sys.modules, "fakepkg.sub", None)


def test_sys_modules_third_party_none_is_clean(monkeypatch):
    monkeypatch.setitem(sys.modules, "not_installed_dep", None)
    with pytest.raises(ImportError):
        import not_installed_dep  # noqa: F401


def test_first_import_is_clean():
    import fakepkg.lazy

    assert fakepkg.lazy.LAZY == 1


# --- F1: reloads ----------------------------------------------------------------


def test_reload():
    importlib.reload(fakepkg.sub)


# --- F1: patch APIs -------------------------------------------------------------


def test_monkeypatch_setattr_module(monkeypatch):
    monkeypatch.setattr(fakepkg, "VALUE", 2)


def test_monkeypatch_setattr_dotted(monkeypatch):
    monkeypatch.setattr("fakepkg.VALUE", 2)


def test_monkeypatch_setattr_class(monkeypatch):
    monkeypatch.setattr(fakepkg.Thing, "x", 2)


def test_mock_patch_dotted():
    with mock.patch("fakepkg.helper", return_value=2):
        pass


@mock.patch.object(fakepkg.sub, "COUNT", 5)
def test_mock_patch_object_decorator():
    pass


def test_patching_non_guarded_objects_is_clean(monkeypatch):
    ns = types.SimpleNamespace(a=1)
    monkeypatch.setattr(ns, "a", 2)
    with mock.patch.object(ns, "a", 3):
        pass
"""

_COLLECT_ENV_TESTS = """
import os

os.environ["FAKEPKG_AT_IMPORT"] = "1"


def test_never_runs():
    pass
"""


def _run_inner(root: Path) -> dict[str, tuple[str, str]]:
    junit = root / "junit.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(root),
            "-p",
            "no:cacheprovider",
            "-q",
            "--continue-on-collection-errors",
            f"--junitxml={junit}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    outcomes: dict[str, tuple[str, str]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        bad = case.find("failure")
        if bad is None:
            bad = case.find("error")
        if bad is None:
            outcomes[case.attrib["name"]] = ("passed", "")
        else:
            outcomes[case.attrib["name"]] = ("failed", f"{bad.attrib.get('message', '')}\n{bad.text or ''}")
    return outcomes


@pytest.fixture(scope="module")
def inner_outcomes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[str, str]]:
    """Run the inner suite once under the guard; map test name -> (outcome, message)."""
    root = tmp_path_factory.mktemp("process-state-guard")
    (root / "conftest.py").write_text(_GUARD_SOURCE.read_text() + _CONFTEST_OVERRIDES)
    (root / "pytest.ini").write_text("[pytest]\n")
    pkg = root / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(_FAKEPKG_INIT)
    (pkg / "sub.py").write_text("COUNT = 0\n")
    (pkg / "lazy.py").write_text("LAZY = 1\n")
    (root / "test_inner.py").write_text(_INNER_TESTS)
    (root / "test_collect_env.py").write_text(_COLLECT_ENV_TESTS)
    return _run_inner(root)


def _assert_fails(outcomes: dict[str, tuple[str, str]], name: str, *needles: str) -> None:
    outcome, message = outcomes[name]
    assert outcome == "failed", f"{name} passed under the guard"
    for needle in needles:
        assert needle in message, f"{name}: {needle!r} not in:\n{message}"
    assert "Refactor to" in message


@pytest.mark.parametrize(
    "name",
    [
        "test_env_subscript_write",
        "test_env_patch_dict",
        "test_env_monkeypatch_setenv",
        "test_env_through_os_dunder_dict",
        "test_env_putenv_unsetenv",
    ],
)
def test_env_writes_of_a_guarded_key_fail(inner_outcomes: dict[str, tuple[str, str]], name: str) -> None:
    _assert_fails(inner_outcomes, name, "[F2]", "FAKEPKG_X")


def test_env_write_in_a_fixture_fails_the_item_at_setup(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(inner_outcomes, "test_env_write_in_fixture_setup", "[F2]", "FAKEPKG_FROM_FIXTURE")


def test_env_write_at_module_import_fails_collection(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(inner_outcomes, "test_collect_env", "[F2]", "FAKEPKG_AT_IMPORT")


@pytest.mark.parametrize(
    "name",
    [
        "test_env_baseline_is_exempt_and_reads_are_clean",
        "test_env_other_keys_are_clean",
        "test_sys_modules_third_party_none_is_clean",
        "test_first_import_is_clean",
        "test_patching_non_guarded_objects_is_clean",
    ],
)
def test_clean_tests_pass(inner_outcomes: dict[str, tuple[str, str]], name: str) -> None:
    assert inner_outcomes[name] == ("passed", "")


@pytest.mark.parametrize(
    "name",
    ["test_sys_modules_swap", "test_sys_modules_patch_dict", "test_sys_modules_monkeypatch_setitem"],
)
def test_sys_modules_swaps_of_a_guarded_module_fail(inner_outcomes: dict[str, tuple[str, str]], name: str) -> None:
    _assert_fails(inner_outcomes, name, "[F1]", "sys.modules['fakepkg.sub']")


def test_reload_of_a_guarded_module_fails(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(inner_outcomes, "test_reload", "[F1]", "re-executed already-imported module", "sub.py")


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("test_monkeypatch_setattr_module", "fakepkg.VALUE"),
        ("test_monkeypatch_setattr_dotted", "'fakepkg.VALUE'"),
        ("test_monkeypatch_setattr_class", "Thing.x"),
        ("test_mock_patch_dotted", "fakepkg.helper"),
        ("test_mock_patch_object_decorator", "fakepkg.sub.COUNT"),
    ],
)
def test_patches_of_guarded_objects_fail(inner_outcomes: dict[str, tuple[str, str]], name: str, needle: str) -> None:
    _assert_fails(inner_outcomes, name, "[F1]", needle)
