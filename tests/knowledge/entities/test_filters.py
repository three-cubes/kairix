"""Unit tests for kairix.knowledge.entities.filters.

Exercises each :class:`SuggestionFilter` strategy in isolation, the
:class:`ChainedSuggestionFilter` composer, and an end-to-end run of the
default chain against the 10 false-positives reported by the dogfood
session on 2026-05-02.

No ``@patch``, no monkeypatch, no private-symbol imports.
"""

from __future__ import annotations

import pytest

from kairix.knowledge.entities.filters import (
    ChainedSuggestionFilter,
    KnownEntityAllowlist,
    NerLabelFilter,
    RolePhraseFilter,
    default_suggestion_filter_chain,
)
from kairix.knowledge.entities.protocols import Suggestion


def _ner(text: str, label: str, confidence: float = 0.8) -> Suggestion:
    """Build an NER-sourced :class:`Suggestion` for fixtures."""
    return {"text": text, "label": label, "source": "ner", "confidence": confidence}


# ---------------------------------------------------------------------------
# RolePhraseFilter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRolePhraseFilter:
    """Drops role-phrase suggestions; keeps real entities."""

    @pytest.mark.unit
    def test_drops_definite_article_role_phrase(self) -> None:
        filt = RolePhraseFilter()
        suggestions = [_ner("the regional team", "ORG")]
        assert filt.apply(suggestions, "in the regional team team") == []

    @pytest.mark.unit
    def test_drops_lowercase_org_phrase(self) -> None:
        filt = RolePhraseFilter()
        suggestions = [_ner("the apac gtm", "ORG")]
        assert filt.apply(suggestions, "the apac gtm reports up") == []

    @pytest.mark.unit
    def test_drops_plain_role_title(self) -> None:
        filt = RolePhraseFilter()
        suggestions = [_ner("Senior Director", "ORG")]
        assert filt.apply(suggestions, "the Senior Director said") == []

    @pytest.mark.unit
    def test_keeps_real_org(self) -> None:
        filt = RolePhraseFilter()
        suggestions = [_ner("ContosoCo", "ORG")]
        assert filt.apply(suggestions, "ContosoCo announced") == suggestions

    @pytest.mark.unit
    def test_returns_new_list(self) -> None:
        """Filter must not mutate its input."""
        filt = RolePhraseFilter()
        suggestions = [_ner("ContosoCo", "ORG"), _ner("the regional lead", "ORG")]
        original = list(suggestions)
        result = filt.apply(suggestions, "ctx")
        assert suggestions == original
        assert result is not suggestions


# ---------------------------------------------------------------------------
# KnownEntityAllowlist
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKnownEntityAllowlist:
    """Promotes allowlist entries that NER missed."""

    @pytest.mark.unit
    def test_promotes_missing_allowlist_entry(self) -> None:
        allow = KnownEntityAllowlist([{"text": "ContosoCo", "label": "ORG"}])
        result = allow.apply([], "ContosoCo announced a new partnership")
        assert len(result) == 1
        assert result[0]["text"] == "ContosoCo"
        assert result[0]["label"] == "ORG"
        assert result[0]["source"] == "allowlist"
        assert result[0]["confidence"] == 1.0

    @pytest.mark.unit
    def test_does_not_duplicate_existing_suggestion(self) -> None:
        allow = KnownEntityAllowlist([{"text": "ContosoCo", "label": "ORG"}])
        existing = [_ner("ContosoCo", "ORG")]
        result = allow.apply(existing, "ContosoCo announced")
        assert len(result) == 1
        # The existing NER entry is preserved untouched.
        assert result[0]["source"] == "ner"

    @pytest.mark.unit
    def test_case_insensitive_context_match(self) -> None:
        allow = KnownEntityAllowlist([{"text": "ContosoCo", "label": "ORG"}])
        result = allow.apply([], "CONTOSOCO announced something")
        assert len(result) == 1
        assert result[0]["text"] == "ContosoCo"

    @pytest.mark.unit
    def test_does_not_promote_when_text_absent_from_context(self) -> None:
        allow = KnownEntityAllowlist([{"text": "AcmeCorp", "label": "ORG"}])
        result = allow.apply([], "no relevant content here")
        assert result == []

    @pytest.mark.unit
    def test_empty_allowlist_passes_through(self) -> None:
        allow = KnownEntityAllowlist([])
        existing = [_ner("ContosoCo", "ORG")]
        assert allow.apply(existing, "ContosoCo") == existing


# ---------------------------------------------------------------------------
# NerLabelFilter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNerLabelFilter:
    """Rule-based relabelling via override sets."""

    @pytest.mark.unit
    def test_person_override_flips_org_to_person(self) -> None:
        filt = NerLabelFilter(person_overrides={"Alex Smith"}, org_overrides=set())
        result = filt.apply([_ner("Alex Smith", "ORG")], "ctx")
        assert result[0]["label"] == "PERSON"

    @pytest.mark.unit
    def test_org_override_flips_person_to_org(self) -> None:
        filt = NerLabelFilter(person_overrides=set(), org_overrides={"AcmeCorp"})
        result = filt.apply([_ner("AcmeCorp", "PERSON")], "ctx")
        assert result[0]["label"] == "ORG"

    @pytest.mark.unit
    def test_unknown_text_passes_through(self) -> None:
        filt = NerLabelFilter(person_overrides={"X"}, org_overrides={"Y"})
        suggestions = [_ner("Microsoft", "ORG")]
        assert filt.apply(suggestions, "ctx") == suggestions

    @pytest.mark.unit
    def test_empty_overrides_pass_through(self) -> None:
        filt = NerLabelFilter(person_overrides=set(), org_overrides=set())
        suggestions = [_ner("ContosoCo", "ORG"), _ner("Jane Doe", "PERSON")]
        assert filt.apply(suggestions, "ctx") == suggestions


# ---------------------------------------------------------------------------
# ChainedSuggestionFilter
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChainedSuggestionFilter:
    """Left-to-right composition of filters."""

    @pytest.mark.unit
    def test_empty_chain_is_pass_through(self) -> None:
        chain = ChainedSuggestionFilter(filters=[])
        suggestions = [_ner("ContosoCo", "ORG")]
        assert chain.apply(suggestions, "ctx") == suggestions

    @pytest.mark.unit
    def test_single_filter_chain_equals_that_filter(self) -> None:
        only = RolePhraseFilter()
        chain = ChainedSuggestionFilter(filters=[only])
        suggestions = [_ner("ContosoCo", "ORG"), _ner("the regional team", "ORG")]
        ctx = "ContosoCo and the regional team met"
        assert chain.apply(suggestions, ctx) == only.apply(suggestions, ctx)

    @pytest.mark.unit
    def test_multi_filter_chain_composes_left_to_right(self) -> None:
        chain = ChainedSuggestionFilter(
            filters=[
                RolePhraseFilter(),
                KnownEntityAllowlist([{"text": "AcmeCorp", "label": "ORG"}]),
                NerLabelFilter(person_overrides={"Alex Smith"}, org_overrides=set()),
            ]
        )
        suggestions = [
            _ner("ContosoCo", "ORG"),
            _ner("the regional team", "ORG"),
            _ner("Alex Smith", "ORG"),
        ]
        ctx = "ContosoCo, the regional team, Alex Smith, and AcmeCorp attended"
        result = chain.apply(suggestions, ctx)

        texts = {s["text"]: s for s in result}
        assert "the regional team" not in texts  # dropped by RolePhraseFilter
        assert texts["ContosoCo"]["label"] == "ORG"
        assert texts["Alex Smith"]["label"] == "PERSON"  # corrected
        assert texts["AcmeCorp"]["source"] == "allowlist"  # promoted
        assert texts["AcmeCorp"]["confidence"] == 1.0


# ---------------------------------------------------------------------------
# End-to-end: 10 dogfood-reported false-positives
# ---------------------------------------------------------------------------


# Outcome categories, expressed as labels we assert in the test.
_DROPPED = "__dropped__"
_PROMOTED_ORG = "__promoted_org__"
_PASS_ORG = "ORG"
_PASS_PERSON = "PERSON"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("phrase", "input_label", "expected"),
    [
        ("the regional team", "ORG", _DROPPED),
        ("Alex Smith", "ORG", _PASS_PERSON),
        ("AcmeCorp", None, _PROMOTED_ORG),
        ("MIT", None, _PROMOTED_ORG),
        ("CIOs", None, _PROMOTED_ORG),
        ("Jane Doe", "PERSON", _PASS_PERSON),
        ("ContosoCo", "ORG", _PASS_ORG),
        ("the regional lead", "ORG", _DROPPED),
        ("Chief Officer", "ORG", _DROPPED),
        ("Microsoft", "ORG", _PASS_ORG),
    ],
)
def test_default_chain_handles_dogfood_false_positives(
    phrase: str,
    input_label: str | None,
    expected: str,
) -> None:
    """Each dogfood-reported phrase yields the expected post-chain outcome."""
    allowlist: list[Suggestion] = [
        {"text": "AcmeCorp", "label": "ORG"},
        {"text": "MIT", "label": "ORG"},
        {"text": "CIOs", "label": "ORG"},
    ]
    person_overrides = {"Alex Smith"}
    org_overrides: set[str] = set()
    chain = default_suggestion_filter_chain(
        allowlist=allowlist,
        person_overrides=person_overrides,
        org_overrides=org_overrides,
    )

    # Build the input suggestion list (skip phrases NER didn't extract).
    suggestions: list[Suggestion] = []
    if input_label is not None:
        suggestions.append(_ner(phrase, input_label))

    # Context is just the phrase itself — sufficient for allowlist scanning
    # and irrelevant to the role/label filters which only inspect text.
    result = chain.apply(suggestions, phrase)

    matches = [s for s in result if s["text"] == phrase]

    if expected == _DROPPED:
        assert matches == [], f"expected {phrase!r} to be dropped, got {matches!r}"
    elif expected == _PROMOTED_ORG:
        assert len(matches) == 1
        assert matches[0]["label"] == "ORG"
        assert matches[0]["source"] == "allowlist"
        assert matches[0]["confidence"] == 1.0
    else:
        # Pass-through with an expected final label.
        assert len(matches) == 1
        assert matches[0]["label"] == expected
