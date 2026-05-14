"""Tests for the top-level kairix package import.

Covers:
  - happy path: __version__ is a non-empty string and the public API
    symbols are importable
  - fallback path: when importlib.metadata.version raises, __version__
    falls back to "0.0.0"
  - guarded imports: when an optional submodule fails to import, the
    package still loads (the symbols just aren't exposed)

Each reload test snapshots sys.modules before and restores it after to
avoid leaking poisoned modules into sibling tests.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


def _snapshot_kairix_modules() -> dict[str, ModuleType | None]:
    """Capture every kairix submodule currently in sys.modules."""
    return {name: sys.modules[name] for name in list(sys.modules) if name == "kairix" or name.startswith("kairix.")}


def _restore_kairix_modules(snapshot: dict[str, ModuleType | None]) -> None:
    """Restore the snapshot of kairix modules to sys.modules."""
    # Remove any kairix modules added during the test
    for name in list(sys.modules):
        if (name == "kairix" or name.startswith("kairix.")) and name not in snapshot:
            sys.modules.pop(name, None)
    # Put back the originals
    for name, mod in snapshot.items():
        if mod is not None:
            sys.modules[name] = mod


@pytest.mark.unit
def test_kairix_version_is_non_empty_string() -> None:
    import kairix

    assert isinstance(kairix.__version__, str)
    assert kairix.__version__  # non-empty


@pytest.mark.unit
def test_public_api_symbols_available() -> None:
    """SearchResult, RetrievalConfig, QueryIntent are importable from kairix."""
    import kairix

    assert hasattr(kairix, "SearchResult")
    assert hasattr(kairix, "RetrievalConfig")
    assert hasattr(kairix, "QueryIntent")


@pytest.mark.unit
def test_version_fallback_when_metadata_missing(monkeypatch) -> None:
    """When importlib.metadata.version raises, __version__ falls back to '0.0.0'.

    We install a fake importlib.metadata.version that always raises, then
    reload the kairix package. No @patch on kairix internals; the fake is
    placed on the standard-library importlib.metadata module.
    """
    import importlib.metadata as _metadata

    def _raise(_name):
        raise _metadata.PackageNotFoundError("not installed")

    monkeypatch.setattr(_metadata, "version", _raise)

    snapshot = _snapshot_kairix_modules()
    try:
        sys.modules.pop("kairix", None)
        kairix_reloaded = importlib.import_module("kairix")
        assert kairix_reloaded.__version__ == "0.0.0"
    finally:
        _restore_kairix_modules(snapshot)


@pytest.mark.unit
def test_search_result_import_failure_swallowed() -> None:
    """When kairix.core.search.pipeline cannot be imported, kairix still loads.

    We pre-poison sys.modules with a sentinel that raises on attribute access,
    then reload kairix. The guarded try/except catches ImportError and
    continues; the SearchResult symbol simply isn't bound.
    """

    class _BrokenModule(ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"simulated import failure for {name}")

    snapshot = _snapshot_kairix_modules()
    try:
        # Remove pipeline + kairix from sys.modules, install poisoned pipeline
        sys.modules.pop("kairix.core.search.pipeline", None)
        sys.modules.pop("kairix", None)
        sys.modules["kairix.core.search.pipeline"] = _BrokenModule("kairix.core.search.pipeline")

        kairix_reloaded = importlib.import_module("kairix")
        assert kairix_reloaded is not None
    finally:
        _restore_kairix_modules(snapshot)


@pytest.mark.unit
def test_retrieval_config_import_failure_swallowed() -> None:
    """When kairix.core.search.config cannot be imported, kairix still loads."""

    class _BrokenModule(ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"simulated import failure for {name}")

    snapshot = _snapshot_kairix_modules()
    try:
        sys.modules.pop("kairix.core.search.config", None)
        sys.modules.pop("kairix", None)
        sys.modules["kairix.core.search.config"] = _BrokenModule("kairix.core.search.config")

        kairix_reloaded = importlib.import_module("kairix")
        assert kairix_reloaded is not None
    finally:
        _restore_kairix_modules(snapshot)


@pytest.mark.unit
def test_query_intent_import_failure_swallowed() -> None:
    """When kairix.core.search.intent cannot be imported, kairix still loads."""

    class _BrokenModule(ModuleType):
        def __getattr__(self, name):
            raise ImportError(f"simulated import failure for {name}")

    snapshot = _snapshot_kairix_modules()
    try:
        sys.modules.pop("kairix.core.search.intent", None)
        sys.modules.pop("kairix", None)
        sys.modules["kairix.core.search.intent"] = _BrokenModule("kairix.core.search.intent")

        kairix_reloaded = importlib.import_module("kairix")
        assert kairix_reloaded is not None
    finally:
        _restore_kairix_modules(snapshot)
