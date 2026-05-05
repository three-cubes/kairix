"""Fixtures for wikilinks tests."""

import pytest

from kairix.paths import _resolve_cached


@pytest.fixture(autouse=True)
def _set_test_roots(monkeypatch):
    """Set document store/workspace roots for wikilinks tests.

    The injector reads these via ``document_root()`` / ``workspace_root()``
    helpers in ``kairix.paths``, which are ``@lru_cache``-d. We use
    ``monkeypatch.setenv`` (environment fixturing — the only mutation
    primitive kairix tests should use) plus ``cache_clear()`` on the
    paths-resolution cache so the new env vars take effect immediately.

    No ``importlib.reload`` and no kairix-code patching — the injector
    refactor (`_eligible_prefixes()` is lazy now) means the env override
    propagates naturally on the next call.

    NOSONAR(python:S5443): /tmp paths are string fixtures used to drive
    path-resolution logic under test — never touched on the filesystem.
    """
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/tmp/test-vault")
    monkeypatch.setenv("KAIRIX_WORKSPACE_ROOT", "/tmp/test-workspaces")
    _resolve_cached.cache_clear()
    yield
    _resolve_cached.cache_clear()
