"""pytest-bdd test module for search_rerank.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "search_rerank.feature")


@pytest.mark.bdd
@scenario(FEATURE, "Enabling re-rank promotes the semantic match to top-1")
def test_rerank_promotes_semantic_match_to_top_1():
    pass
