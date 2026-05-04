"""Contract tests: SuggestionFilter protocol conformance.

Verifies that every public filter strategy (and a small in-test fake)
satisfies the :class:`SuggestionFilter` protocol via ``isinstance()``.
"""

from __future__ import annotations

import pytest

from kairix.knowledge.entities.filters import (
    ChainedSuggestionFilter,
    KnownEntityAllowlist,
    NerLabelFilter,
    RolePhraseFilter,
)
from kairix.knowledge.entities.protocols import Suggestion, SuggestionFilter


@pytest.mark.contract
class TestSuggestionFilterProtocolCompliance:
    """All public filter classes satisfy SuggestionFilter."""

    @pytest.mark.contract
    def test_role_phrase_filter_satisfies_protocol(self) -> None:
        assert isinstance(RolePhraseFilter(), SuggestionFilter)

    @pytest.mark.contract
    def test_known_entity_allowlist_satisfies_protocol(self) -> None:
        assert isinstance(KnownEntityAllowlist([]), SuggestionFilter)

    @pytest.mark.contract
    def test_ner_label_filter_satisfies_protocol(self) -> None:
        assert isinstance(NerLabelFilter(set(), set()), SuggestionFilter)

    @pytest.mark.contract
    def test_chained_suggestion_filter_satisfies_protocol(self) -> None:
        assert isinstance(ChainedSuggestionFilter(filters=[]), SuggestionFilter)

    @pytest.mark.contract
    def test_in_test_fake_satisfies_protocol(self) -> None:
        """An ad-hoc fake implementing apply() satisfies the protocol."""

        class FakeFilter:
            def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]:
                del context
                return list(suggestions)

        assert isinstance(FakeFilter(), SuggestionFilter)
