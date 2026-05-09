"""Tests for path-based gold title matching in benchmark runner."""

from __future__ import annotations

import pytest

from kairix.quality.eval.gold_builder import path_title
from kairix.quality.eval.metrics import match_gold_to_path
from kairix.quality.eval.metrics import relevance_for_path as _relevance_for_path

pytestmark = pytest.mark.unit


class TestPathTitle:
    def test_generates_path_based_title(self) -> None:
        # Every segment is preserved so distinct paths can never collide on the
        # same title — including a generic filename in different collections.
        result = path_title("reference-library/engineering/adr-examples/readme.md")
        assert result == "reference-library/engineering/adr-examples/readme"

    def test_short_path(self) -> None:
        result = path_title("docs/readme.md")
        assert result == "docs/readme"

    def test_single_segment(self) -> None:
        result = path_title("readme.md")
        assert result == "readme"


class TestMatchGoldToPath:
    def test_path_based_matches_correct_file(self) -> None:
        assert match_gold_to_path(
            "engineering/adr-examples/readme",
            "reference-library/engineering/adr-examples/readme.md",
        )

    def test_path_based_rejects_different_file_same_stem(self) -> None:
        assert not match_gold_to_path(
            "engineering/adr-examples/readme",
            "data-and-analysis/dbt-docs/readme.md",
        )

    def test_stem_only_matches(self) -> None:
        assert match_gold_to_path("patterns", "vault/knowledge/patterns.md")

    def test_stem_only_rejects_different_stem(self) -> None:
        assert not match_gold_to_path("patterns", "vault/knowledge/anti-patterns.md")

    def test_case_insensitive(self) -> None:
        assert match_gold_to_path(
            "Engineering/ADR-Examples/README",
            "reference-library/engineering/adr-examples/readme.md",
        )

    def test_hyphen_underscore_normalisation(self) -> None:
        assert match_gold_to_path(
            "engineering/adr_examples/read_me",
            "reference-library/engineering/adr-examples/read-me.md",
        )


class TestRelevanceForPath:
    def test_returns_relevance_for_matching_path(self) -> None:
        gold = [
            {"title": "engineering/adr-examples/readme", "relevance": 2},
            {"title": "data-and-analysis/dbt-docs/readme", "relevance": 1},
        ]
        assert _relevance_for_path("reference-library/engineering/adr-examples/readme.md", gold) == 2

    def test_returns_zero_for_non_matching(self) -> None:
        gold = [{"title": "engineering/adr-examples/readme", "relevance": 2}]
        assert _relevance_for_path("totally-different/file.md", gold) == 0

    def test_stem_only_backwards_compat(self) -> None:
        gold = [{"title": "patterns", "relevance": 2}]
        assert _relevance_for_path("vault/knowledge/patterns.md", gold) == 2
