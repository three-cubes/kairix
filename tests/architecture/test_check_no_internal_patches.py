"""F1 detector tests — verify all six internal-substitution shapes are caught.

The F1 detector (``scripts/checks/check_no_internal_patches.py``) flags
six structurally-identical shapes of internal patching:

1. ``@patch("kairix.X.Y", ...)`` — decorator form
2. ``with patch("kairix.X.Y", ...)`` — context manager form
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where alias resolves to a kairix module
5. ``monkeypatch.setattr("kairix.X.Y", ...)`` — string-target form
6. ``monkeypatch.setattr(<kairix module ref>, ...)`` — ref-target form

Each shape gets a positive test (kairix target → violation flagged) and
a negative test (stdlib / external SDK target → ignored). Tests drive
``file_has_internal_patch`` through the public detector surface; no
private import.

To add coverage for a new shape: append a positive/negative pair below
and extend the detector to satisfy both. Sabotage-prove by commenting
out the detector branch, running the relevant positive test, confirming
red, restoring, confirming green.
"""

from __future__ import annotations

# Add scripts/checks to sys.path so the detector module is importable.
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_internal_patches import file_has_internal_patch  # noqa: E402

pytestmark = pytest.mark.unit


def _write_test_module(tmp_path: Path, source: str) -> Path:
    """Write ``source`` to a temporary .py file and return the path."""
    f = tmp_path / "sample_test.py"
    f.write_text(source, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Shape 1: @patch decorator
# ---------------------------------------------------------------------------


def test_detects_at_patch_decorator_on_kairix_target(tmp_path: Path) -> None:
    """A decorator ``@patch("kairix.X.Y")`` is a violation.

    To fix: rewrite the test to construct the unit under test with a
    Fake* from ``tests/fakes.py``.
    """
    src = """
from unittest.mock import patch

@patch("kairix.core.search.bm25.bm25_search")
def test_something(mock_search):
    pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_at_patch_decorator_on_stdlib_target(tmp_path: Path) -> None:
    """A decorator ``@patch("os.path.exists")`` is allowed.

    Stdlib boundaries are exempt — patching at the kairix edge is
    fixturing genuinely external state.
    """
    src = """
from unittest.mock import patch

@patch("os.path.exists")
def test_something(mock_exists):
    pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 2: with patch context manager
# ---------------------------------------------------------------------------


def test_detects_with_patch_context_on_kairix_target(tmp_path: Path) -> None:
    """A context manager ``with patch("kairix.X.Y")`` is a violation.

    To fix: rewrite the test to construct the unit under test with a
    Fake* from ``tests/fakes.py``.
    """
    src = """
from unittest.mock import patch

def test_something():
    with patch("kairix.core.search.bm25.bm25_search"):
        pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_with_patch_context_on_external_target(tmp_path: Path) -> None:
    """``with patch("httpx.Client")`` is allowed.

    External SDK boundaries are exempt.
    """
    src = """
from unittest.mock import patch

def test_something():
    with patch("httpx.Client"):
        pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 3: Full-path attribute assignment ``kairix.X.Y = <expr>``
# ---------------------------------------------------------------------------


def test_detects_full_path_attribute_assignment_on_kairix(tmp_path: Path) -> None:
    """Direct reassignment ``kairix.paths.provider_name = lambda: "fake"`` is a violation.

    To fix: move the dependency to construction-time via a ``*Deps``
    dataclass and inject a Fake* through that seam.
    """
    src = """
import kairix.paths

def test_something():
    saved = kairix.paths.provider_name
    kairix.paths.provider_name = lambda: "fake"
    try:
        pass
    finally:
        kairix.paths.provider_name = saved
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_full_path_attribute_assignment_on_external(tmp_path: Path) -> None:
    """``httpx.Client = FakeClient`` is allowed — external SDK boundary."""
    src = """
import httpx

def test_something():
    httpx.Client = lambda *a, **k: None
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 4: Aliased attribute assignment ``<alias>.Y = <expr>``
# ---------------------------------------------------------------------------


def test_detects_aliased_attribute_assignment_via_import_as(tmp_path: Path) -> None:
    """Aliased reassignment via ``import kairix.paths as paths_mod`` is a violation.

    To fix: refactor the production function-local imports to inject
    through a constructor parameter, then pass a Fake* via that seam.
    """
    src = """
import kairix.paths as paths_mod

def test_something():
    saved = paths_mod.provider_name
    paths_mod.provider_name = lambda: "fake"
    try:
        pass
    finally:
        paths_mod.provider_name = saved
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_detects_aliased_attribute_assignment_via_from_import_as(tmp_path: Path) -> None:
    """Aliased reassignment via ``from kairix import providers as providers_mod`` is a violation.

    To fix: use the canonical Provider Protocol seam — construct the
    unit under test with a ``FakeProvider`` from ``tests/fakes.py``.
    """
    src = """
from kairix import providers as providers_mod

def test_something():
    providers_mod.get_provider = lambda name, registry=None: object()
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_detects_aliased_attribute_assignment_via_from_import(tmp_path: Path) -> None:
    """``from kairix.providers import _base; _base.Provider = ...`` is a violation.

    To fix: use the Protocol Fake from ``tests/fakes.py``; the
    production module's namespace is not a test seam.
    """
    src = """
from kairix.providers import _base

def test_something():
    _base.Provider = object
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_aliased_attribute_assignment_on_non_kairix(tmp_path: Path) -> None:
    """``import os as os_mod; os_mod.environ = {}`` is allowed — stdlib."""
    src = """
import os as os_mod

def test_something():
    os_mod.environ = {}
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 5: monkeypatch.setattr with string target
# ---------------------------------------------------------------------------


def test_detects_monkeypatch_setattr_string_target_on_kairix(tmp_path: Path) -> None:
    """``monkeypatch.setattr("kairix.X.Y", fake)`` is a violation.

    To fix: rewrite to construct the unit under test with a Fake* from
    ``tests/fakes.py`` via the appropriate DI seam.
    """
    src = """
def test_something(monkeypatch):
    monkeypatch.setattr("kairix.paths.provider_name", lambda: "fake")
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_monkeypatch_setattr_string_target_on_stdlib(tmp_path: Path) -> None:
    """``monkeypatch.setattr("os.environ", {})`` is allowed — stdlib boundary."""
    src = """
def test_something(monkeypatch):
    monkeypatch.setattr("os.environ", {})
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 6: monkeypatch.setattr with module-ref target
# ---------------------------------------------------------------------------


def test_detects_monkeypatch_setattr_ref_target_on_kairix(tmp_path: Path) -> None:
    """``monkeypatch.setattr(kairix_module, "attr", fake)`` is a violation.

    To fix: inject a Fake* through the constructor seam; the imported
    module is not a test seam.
    """
    src = """
import kairix.paths as paths_mod

def test_something(monkeypatch):
    monkeypatch.setattr(paths_mod, "provider_name", lambda: "fake")
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_detects_monkeypatch_setattr_ref_target_on_full_path(tmp_path: Path) -> None:
    """``monkeypatch.setattr(kairix.paths, "attr", fake)`` is a violation.

    To fix: route the dependency through a Protocol seam and pass the
    Fake* at construction.
    """
    src = """
import kairix.paths

def test_something(monkeypatch):
    monkeypatch.setattr(kairix.paths, "provider_name", lambda: "fake")
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_monkeypatch_setattr_ref_target_on_external(tmp_path: Path) -> None:
    """``monkeypatch.setattr(httpx, "Client", FakeClient)`` is allowed — external SDK."""
    src = """
import httpx

def test_something(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: None)
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Sanity coverage
# ---------------------------------------------------------------------------


def test_clean_file_returns_false(tmp_path: Path) -> None:
    """A test that injects a Fake* via DI shows no violation."""
    src = """
from tests.fakes import FakeProvider

def test_something():
    provider = FakeProvider(chat_reply="ok")
    assert provider.chat([]) == "ok"
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


def test_empty_file_returns_false(tmp_path: Path) -> None:
    """An empty file shows no violation."""
    f = tmp_path / "empty.py"
    f.write_text("", encoding="utf-8")
    assert file_has_internal_patch(f) is False
