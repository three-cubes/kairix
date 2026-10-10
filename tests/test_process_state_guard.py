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
BASELINE_CONFTEST = Path(__file__).resolve()  # this throwaway conftest is the root one

# Imported before collection, so the collection-time cases below replace /
# remove an entry that existed when their module started collecting.
import fakepkg.removeme  # noqa: E402
import fakepkg.swapme  # noqa: E402,F401


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


@pytest.fixture
def swapped_in_setup():
    original = sys.modules["fakepkg.sub"]
    sys.modules["fakepkg.sub"] = types.ModuleType("fakepkg.sub")
    yield
    sys.modules["fakepkg.sub"] = original


def test_sys_modules_swap_in_setup(swapped_in_setup):
    pass  # never runs: the guard fails the item at the end of setup


@pytest.fixture
def injected_entry():
    name = "fake" + "pkg.injected"  # a computed key
    yield name
    sys.modules.pop(name, None)


def test_sys_modules_computed_key_insertion(injected_entry):
    sys.modules[injected_entry] = types.ModuleType(injected_entry)


@pytest.fixture
def restore_environ():
    original = os.environ
    yield dict(original)
    os.environ = original


def test_env_replaced_directly(restore_environ):
    os.environ = restore_environ


def test_env_replaced_by_monkeypatch(monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))


def test_env_replaced_by_mock_patch():
    with mock.patch("os.environ", dict(os.environ)):
        pass


def test_env_deleted_by_monkeypatch(monkeypatch):
    monkeypatch.delattr(os, "environ")


def test_env_is_back_after_the_deletion():
    assert isinstance(os.environ, os._Environ)
    assert "PATH" in os.environ or "Path" in os.environ


def test_env_patch_dict_of_other_keys_is_clean():
    with mock.patch.dict(os.environ, {"OTHER": "1"}):
        assert os.environ["OTHER"] == "1"


@pytest.fixture
def misnamed_entry():
    yield "fakepkg.computed"
    sys.modules.pop("fakepkg.computed", None)


def test_sys_modules_real_module_under_another_name(misnamed_entry):
    sys.modules[misnamed_entry] = fakepkg


def test_baseline_exemption_from_a_test_module(monkeypatch):
    from conftest import allow_baseline_writes as exempt

    with exempt():
        os.environ["FAKEPKG_ALIAS"] = "1"
    del os.environ["FAKEPKG_ALIAS"]


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


class _EqualityRaises:
    a = 1

    def __eq__(self, other):
        raise RuntimeError("__eq__ must not be called by the guard")

    __hash__ = object.__hash__


def test_patching_an_object_whose_eq_raises_is_clean(monkeypatch):
    target = _EqualityRaises()
    monkeypatch.setattr(target, "a", 2)
    with mock.patch.object(target, "a", 3):
        pass
"""

_COLLECT_ENV_TESTS = """
import os

os.environ["FAKEPKG_AT_IMPORT"] = "1"


def test_never_runs():
    pass
"""

_COLLECT_RELOAD_TESTS = """
import importlib

import fakepkg.sub

importlib.reload(fakepkg.sub)


def test_never_runs():
    pass
"""

# Runs last (file name order): its teardown swap leaks into nothing after it.
# Each collection-time case uses its own submodule, so its leak touches no
# other test (every module is collected before any test runs).
_COLLECT_SWAP_TESTS = """
import sys
import types

sys.modules["fakepkg." + "swapme"] = types.ModuleType("fakepkg.swapme")  # computed key


def test_never_runs():
    pass
"""

_COLLECT_REMOVAL_TESTS = """
import sys

del sys.modules["fakepkg." + "removeme"]


def test_never_runs():
    pass
"""

_COLLECT_GENUINE_IMPORT_TESTS = """
import fakepkg.collected


def test_collected_module_is_usable():
    assert fakepkg.collected.COLLECTED == 1
"""

_TEARDOWN_SWAP_TESTS = """
import sys
import types

import pytest

import fakepkg.sub


@pytest.fixture
def swaps_in_teardown():
    yield
    sys.modules["fakepkg.sub"] = types.ModuleType("fakepkg.sub")


def test_sys_modules_swap_in_teardown(swaps_in_teardown):
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
    for name in ("swapme", "removeme"):
        (pkg / f"{name}.py").write_text("X = 1\n")
    (pkg / "collected.py").write_text("COLLECTED = 1\n")
    (root / "test_inner.py").write_text(_INNER_TESTS)
    (root / "test_collect_env.py").write_text(_COLLECT_ENV_TESTS)
    (root / "test_collect_reload.py").write_text(_COLLECT_RELOAD_TESTS)
    (root / "test_zz_teardown_swap.py").write_text(_TEARDOWN_SWAP_TESTS)
    (root / "test_collect_swap.py").write_text(_COLLECT_SWAP_TESTS)
    (root / "test_collect_removal.py").write_text(_COLLECT_REMOVAL_TESTS)
    (root / "test_collect_genuine_import.py").write_text(_COLLECT_GENUINE_IMPORT_TESTS)
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
        "test_env_patch_dict_of_other_keys_is_clean",
        "test_patching_an_object_whose_eq_raises_is_clean",
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


def test_swap_undone_before_the_call_is_caught_at_setup(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    """A fixture's swap is checked at the end of setup, not only after the call."""
    _assert_fails(inner_outcomes, "test_sys_modules_swap_in_setup", "[F1]", "failed on setup", "'fakepkg.sub'")


def test_swap_in_a_teardown_finalizer_is_caught(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(inner_outcomes, "test_sys_modules_swap_in_teardown", "[F1]", "failed on teardown", "'fakepkg.sub'")


def test_inserted_entry_that_the_import_machinery_did_not_make_fails(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    _assert_fails(
        inner_outcomes,
        "test_sys_modules_computed_key_insertion",
        "[F1]",
        "sys.modules['fakepkg.injected'] inserted without the import machinery",
    )


def test_baseline_exemption_entered_outside_the_root_conftest_fails(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    """An aliased import of allow_baseline_writes() from a test module grants
    nothing: the call itself and the write it tried to cover both fail."""
    _assert_fails(
        inner_outcomes,
        "test_baseline_exemption_from_a_test_module",
        "[F2]",
        "allow_baseline_writes() entered from",
        "set FAKEPKG_ALIAS",
    )


def test_reload_at_collection_time_fails_collection(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(inner_outcomes, "test_collect_reload", "[F1]", "re-executed already-imported module", "sub.py")


def test_sys_modules_replacement_at_collection_time_fails_collection(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    _assert_fails(inner_outcomes, "test_collect_swap", "[F1]", "sys.modules['fakepkg.swapme'] replaced")


def test_sys_modules_removal_at_collection_time_fails_collection(
    inner_outcomes: dict[str, tuple[str, str]],
) -> None:
    _assert_fails(inner_outcomes, "test_collect_removal", "[F1]", "sys.modules['fakepkg.removeme'] removed")


def test_genuine_import_at_collection_time_is_clean(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    assert inner_outcomes["test_collected_module_is_usable"] == ("passed", "")


@pytest.mark.parametrize(
    "name",
    ["test_env_replaced_directly", "test_env_replaced_by_monkeypatch", "test_env_replaced_by_mock_patch"],
)
def test_wholesale_environ_replacement_fails(inner_outcomes: dict[str, tuple[str, str]], name: str) -> None:
    _assert_fails(inner_outcomes, name, "[F2]", "os.environ replaced wholesale")


def test_deleting_environ_fails_as_f2_and_is_restored(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    """``monkeypatch.delattr(os, "environ")`` leaves no ``os.environ`` for the
    end-of-phase identity check (or pytest's own reporting) to read: the guard
    puts the snapshotted mapping back and fails the test normally, and the
    next inner test still sees the real ``os.environ``.

    Sabotage proof (executed): read ``os.environ`` directly in ``_end_phase``
    → the inner run dies with an AttributeError traceback, the junit entry is
    an internal error without the F2 message and this fails; restored.
    """
    _assert_fails(inner_outcomes, "test_env_deleted_by_monkeypatch", "[F2]", "os.environ deleted")
    assert inner_outcomes["test_env_is_back_after_the_deletion"] == ("passed", "")


def test_real_module_inserted_under_another_name_fails(inner_outcomes: dict[str, tuple[str, str]]) -> None:
    _assert_fails(
        inner_outcomes,
        "test_sys_modules_real_module_under_another_name",
        "[F1]",
        "sys.modules['fakepkg.computed'] inserted without the import machinery",
    )
