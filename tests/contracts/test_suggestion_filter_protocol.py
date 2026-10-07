"""Contract tests: SuggestionFilter protocol conformance.

Verifies that every public filter strategy AND the canonical
:class:`tests.fakes.FakeSuggestionFilter` satisfy the
:class:`SuggestionFilter` protocol via ``isinstance()`` and honour the
shared ``apply`` return shape — one parametrized body (F43).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.knowledge.entities.filters import (
    ChainedSuggestionFilter,
    KnownEntityAllowlist,
    NerLabelFilter,
    RolePhraseFilter,
)
from kairix.knowledge.entities.protocols import Suggestion, SuggestionFilter
from tests.fakes import FakeSuggestionFilter

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "factory",
    [
        RolePhraseFilter,
        lambda: KnownEntityAllowlist([]),
        lambda: NerLabelFilter(set(), set()),
        lambda: ChainedSuggestionFilter(filters=[]),
        FakeSuggestionFilter,
    ],
    ids=["role_phrase", "known_entity_allowlist", "ner_label", "chained", "fake"],
)
def test_filter_satisfies_protocol(factory: Callable[[], SuggestionFilter]) -> None:
    """Every filter is a runtime :class:`SuggestionFilter`; a neutral
    suggestion passes through ``apply`` as a NEW list (never the input
    list object, never None).

    Sabotage proof: rename ``RolePhraseFilter.apply`` to ``apply_x``; the
    ``role_phrase`` leg's isinstance check fails. Restored.
    """
    flt = factory()
    assert isinstance(flt, SuggestionFilter)
    neutral: list[Suggestion] = [{"text": "Acme", "label": "ORG", "source": "ner", "confidence": 0.9}]
    out = flt.apply(neutral, "Acme announced results")
    assert out is not neutral
    assert [s["text"] for s in out] == ["Acme"]
