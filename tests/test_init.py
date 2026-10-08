"""Tests for the top-level kairix package import.

Covers:
  - happy path: __version__ is a non-empty string and the public API
    symbols are importable
  - fallback path: when the distribution-metadata lookup raises,
    __version__ falls back to "0.0.0"
  - guarded imports: when an optional submodule fails to import, the
    package still loads (the symbols just aren't exposed)

The fallback and guarded-import paths are driven through the
``PackageInitDeps`` seam in ``kairix.package_meta`` (the code
``kairix/__init__.py`` runs at import time) with a failing lookup /
importer — never by poisoning ``sys.modules`` and re-importing the
package (F1).
"""

from __future__ import annotations

import importlib
import importlib.metadata
from types import ModuleType
from typing import Any

import pytest

from kairix.package_meta import (
    DISTRIBUTION_NAME,
    FALLBACK_VERSION,
    PUBLIC_API,
    PackageInitDeps,
    bind_public_api,
    resolve_version,
)

pytestmark = pytest.mark.unit


def _importer_failing_for(broken_module: str) -> Any:
    """An importer that raises ImportError for ``broken_module`` only."""

    def _import(name: str) -> ModuleType:
        if name == broken_module:
            raise ImportError(f"simulated import failure for {name}")
        return importlib.import_module(name)

    return _import


def test_kairix_version_is_non_empty_string() -> None:
    import kairix

    assert isinstance(kairix.__version__, str)
    assert kairix.__version__  # non-empty


def test_public_api_symbols_available() -> None:
    """SearchResult, RetrievalConfig, QueryIntent are importable from kairix."""
    import kairix

    assert hasattr(kairix, "SearchResult")
    assert hasattr(kairix, "RetrievalConfig")
    assert hasattr(kairix, "QueryIntent")


def test_version_reads_the_distribution_metadata_name() -> None:
    """The lookup is asked for the real distribution name, not "kairix" (#267).

    Sabotage proof (executed): change ``DISTRIBUTION_NAME`` lookup in
    ``resolve_version`` to ``deps.version_lookup("kairix")`` → the
    recorded name no longer matches; restored.
    """
    asked: list[str] = []

    def _lookup(name: str) -> str:
        asked.append(name)
        return "2026.10.9"

    assert resolve_version(PackageInitDeps(version_lookup=_lookup)) == "2026.10.9"
    assert asked == [DISTRIBUTION_NAME] == ["Kairix-agentic-knowledge-mgt"]


def test_version_fallback_when_metadata_missing() -> None:
    """When the metadata lookup raises, __version__ falls back to '0.0.0'.

    Sabotage proof (executed): drop the ``except`` arm in
    ``resolve_version`` → ``PackageNotFoundError`` propagates; restored.
    """

    def _raise(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("not installed")

    assert resolve_version(PackageInitDeps(version_lookup=_raise)) == FALLBACK_VERSION == "0.0.0"


def test_default_deps_resolve_the_installed_version() -> None:
    """With no deps injected the production stdlib lookup runs and matches
    the version the package exposed at import time."""
    import kairix

    assert resolve_version() == kairix.__version__


def test_bind_public_api_binds_every_symbol_by_default() -> None:
    """The production importer binds all three public symbols."""
    namespace: dict[str, Any] = {}
    bound = bind_public_api(namespace)
    assert bound == ["SearchResult", "RetrievalConfig", "QueryIntent"]
    import kairix

    for _module, symbol in PUBLIC_API:
        assert namespace[symbol] is getattr(kairix, symbol)


@pytest.mark.parametrize(("broken_module", "missing_symbol"), PUBLIC_API)
def test_import_failure_of_one_public_module_is_swallowed(broken_module: str, missing_symbol: str) -> None:
    """When one home module cannot be imported, binding still succeeds for
    the others and the failing symbol is simply not bound.

    Covers the three guarded imports (pipeline / config / intent) that the
    old tests exercised by poisoning ``sys.modules``.

    Sabotage proof (executed): remove the ``except (ImportError, ...)``
    arm in ``bind_public_api`` → the simulated ImportError propagates and
    every parametrised case fails; restored.
    """
    namespace: dict[str, Any] = {}
    deps = PackageInitDeps(import_module=_importer_failing_for(broken_module))

    bound = bind_public_api(namespace, deps)

    assert missing_symbol not in namespace
    assert missing_symbol not in bound
    expected = [symbol for _module, symbol in PUBLIC_API if symbol != missing_symbol]
    assert bound == expected
    assert sorted(namespace) == sorted(expected)
