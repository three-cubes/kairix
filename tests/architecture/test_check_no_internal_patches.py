"""F1 detector tests — covers @patch, with patch, and attribute-reassignment shapes.

The F1 detector (``scripts/checks/check_no_internal_patches.py``) was
originally written to catch ``@patch("kairix.X.Y")`` and
``with patch("kairix.X.Y")``. It missed the equivalent attribute-
reassignment shapes — ``module.attr = fake``, ``monkeypatch.setattr(
module, "attr", fake)``, etc. — which are structurally identical
violations of "don't substitute kairix internals in tests" and which
proliferated across the test suite while F1 was narrow.

Six shapes the detector MUST flag (each exercised below):

1. ``@patch("kairix.X.Y", ...)`` — pre-existing
2. ``with patch("kairix.X.Y", ...)`` — pre-existing
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where alias resolves to a kairix module —
   `import kairix.paths as paths_mod; paths_mod.fn = ...`
5. ``monkeypatch.setattr("kairix.X.Y", ...)`` — string-target form
6. ``monkeypatch.setattr(<kairix module ref>, ...)`` — ref-target form

Plus the negative case for each: the same shape against an exempt
target (stdlib, external SDK) must NOT fire.

All tests are exercised via the ``file_has_internal_patch`` import
from the detector module — driving the public surface.
"""

from __future__ import annotations

# Add the scripts/checks directory to sys.path so we can import the detector.
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
# Shape 1: @patch decorator (pre-existing detection)
# ---------------------------------------------------------------------------


def test_detects_at_patch_decorator_on_kairix_target(tmp_path: Path) -> None:
    """``@patch("kairix.X.Y")`` is the pre-existing F1 violation."""
    src = """
from unittest.mock import patch

@patch("kairix.core.search.bm25.bm25_search")
def test_something(mock_search):
    pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_at_patch_decorator_on_stdlib_target(tmp_path: Path) -> None:
    """``@patch("os.path.exists")`` is allowed — stdlib boundary."""
    src = """
from unittest.mock import patch

@patch("os.path.exists")
def test_something(mock_exists):
    pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


# ---------------------------------------------------------------------------
# Shape 2: with patch context manager (pre-existing detection)
# ---------------------------------------------------------------------------


def test_detects_with_patch_context_on_kairix_target(tmp_path: Path) -> None:
    """``with patch("kairix.X.Y")`` is the pre-existing F1 violation."""
    src = """
from unittest.mock import patch

def test_something():
    with patch("kairix.core.search.bm25.bm25_search"):
        pass
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_ignores_with_patch_context_on_external_target(tmp_path: Path) -> None:
    """``with patch("httpx.Client")`` is allowed — external SDK boundary."""
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
    """``kairix.paths.provider_name = lambda: "fake"`` — direct reassignment.

    NEW DETECTION. This shape doesn't use ``patch`` or ``monkeypatch`` —
    just rebinds the module attribute. Structurally equivalent to
    ``@patch("kairix.paths.provider_name", ...)``; semantically the
    same anti-pattern.
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
    """``import kairix.paths as paths_mod; paths_mod.fn = ...`` — aliased.

    NEW DETECTION. The alias resolves via the file's import map to
    ``kairix.paths``; reassigning ``paths_mod.fn`` is reassigning
    ``kairix.paths.fn``.
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
    """``from kairix import providers as providers_mod; providers_mod.get_provider = ...``"""
    src = """
from kairix import providers as providers_mod

def test_something():
    providers_mod.get_provider = lambda name, registry=None: object()
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_detects_aliased_attribute_assignment_via_from_import(tmp_path: Path) -> None:
    """``from kairix.providers import _base; _base.Provider = ...`` — no-as form."""
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
    """``monkeypatch.setattr("kairix.X.Y", fake)`` — string target.

    NEW DETECTION. Same semantic as @patch but via the pytest fixture
    rather than the decorator.
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
    """``monkeypatch.setattr(kairix_module, "attr", fake)`` — ref target.

    NEW DETECTION. First positional is a Name/Attribute resolving to a
    kairix module rather than a string.
    """
    src = """
import kairix.paths as paths_mod

def test_something(monkeypatch):
    monkeypatch.setattr(paths_mod, "provider_name", lambda: "fake")
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is True


def test_detects_monkeypatch_setattr_ref_target_on_full_path(tmp_path: Path) -> None:
    """``monkeypatch.setattr(kairix.paths, "attr", fake)`` — full-path ref."""
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
# Sanity: clean file is clean
# ---------------------------------------------------------------------------


def test_clean_file_returns_false(tmp_path: Path) -> None:
    """A file that uses DI seams (no patching) reports no violation."""
    src = """
from tests.fakes import FakeProvider

def test_something():
    provider = FakeProvider(chat_reply="ok")
    assert provider.chat([]) == "ok"
"""
    assert file_has_internal_patch(_write_test_module(tmp_path, src)) is False


def test_empty_file_returns_false(tmp_path: Path) -> None:
    """An empty file is not a violation."""
    f = tmp_path / "empty.py"
    f.write_text("", encoding="utf-8")
    assert file_has_internal_patch(f) is False
