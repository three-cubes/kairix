"""Fixtures for wikilinks tests.

Provides ``paths`` and matching path-string fixtures so wikilinks tests
construct a real ``KairixPaths`` (via ``tests.fakes.FakePaths``) and inject
it through the production code's ``paths=`` parameter — no env-var
monkeypatching, no ``_resolve_cached.cache_clear()``.

Tests that don't need path injection don't have to consume these fixtures;
tests that do should declare ``paths`` (and ``test_vault_root`` /
``test_workspaces_root`` for path-string concatenation in scenario data).
"""

from __future__ import annotations

import pytest

from kairix.paths import KairixPaths
from tests.fakes import FakePaths


@pytest.fixture
def paths() -> KairixPaths:
    """A ``KairixPaths`` with sentinel test roots — no filesystem I/O.

    The ``/var/lib/kairix-test/`` prefix is a non-publicly-writable
    sentinel: the strings exercise prefix-matching logic in
    ``should_inject`` and ``inject_wikilinks``, but nothing under these
    paths is ever read or written.
    """
    return FakePaths(
        document_root="/var/lib/kairix-test/vault",
        workspace_root="/var/lib/kairix-test/workspaces",
    )


@pytest.fixture
def test_vault_root(paths: KairixPaths) -> str:
    """String form of ``paths.document_root`` for f-string path construction in tests."""
    return str(paths.document_root)


@pytest.fixture
def test_workspaces_root(paths: KairixPaths) -> str:
    """String form of ``paths.workspace_root`` for f-string path construction in tests."""
    return str(paths.workspace_root)
