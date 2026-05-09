"""pytest-bdd binding for search_collection_retrieval_overrides.feature."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario(
    "features/search_collection_retrieval_overrides.feature",
    "A collection-level retrieval override merges over the global config",
)
def test_per_collection_override_merges() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/search_collection_retrieval_overrides.feature",
    "Searches against unconfigured collections still get the global default",
)
def test_unconfigured_collection_uses_global() -> None:
    pass


@pytest.mark.bdd
@scenario(
    "features/search_collection_retrieval_overrides.feature",
    "Multi-collection searches do NOT apply per-collection overrides",
)
def test_multi_collection_uses_global() -> None:
    pass
