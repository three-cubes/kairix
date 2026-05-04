"""Integration tests for suggest_entities + ChainedSuggestionFilter.

Closes the 2026-05-02 dogfood-reported bug: kairix entity suggest mistypes
role phrases as ORG, miscategorises persons as ORG, and misses obvious
orgs the NER model doesn't know.

The fix wires default_suggestion_filter_chain() into suggest_entities so
role phrases drop, an injected allowlist promotes missing orgs, and
NerLabelFilter overrides correct mistypes.

Tested through suggest_entities's public surface using a small fake nlp
pipeline + the FakeNeo4jClient. No @patch, no monkeypatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.knowledge.entities.filters import (
    ChainedSuggestionFilter,
    KnownEntityAllowlist,
    NerLabelFilter,
    RolePhraseFilter,
)
from kairix.knowledge.entities.suggest import suggest_entities
from tests.fixtures.neo4j_mock import FakeNeo4jClient


class _FakeEntity:
    def __init__(self, text: str, label: str) -> None:
        self.text = text
        self.label_ = label


class _FakeSentence:
    def __init__(self, text: str, ents: list[_FakeEntity]) -> None:
        self.text = text
        self.ents = ents


class _FakeDoc:
    def __init__(self, sents: list[_FakeSentence]) -> None:
        self.sents = sents


class _FakeNlp:
    """Tiny spaCy-shaped pipeline: maps an input string to a configured doc."""

    def __init__(self, doc: _FakeDoc) -> None:
        self._doc = doc

    def __call__(self, text: str) -> _FakeDoc:
        return self._doc


def _doc_with_entities(text: str, entities: list[tuple[str, str]]) -> _FakeDoc:
    return _FakeDoc(sents=[_FakeSentence(text=text, ents=[_FakeEntity(t, l) for t, l in entities])])


@pytest.mark.unit
def test_role_phrase_dropped_by_default_chain() -> None:
    """The dogfood failure: 'the APAC GTM' tagged as ORG should be dropped by RolePhraseFilter."""
    nlp = _FakeNlp(_doc_with_entities("the APAC GTM is hiring", [("the APAC GTM", "ORG")]))
    neo4j = FakeNeo4jClient(entities=[])

    result = suggest_entities("the APAC GTM is hiring", neo4j, nlp=nlp)

    # Default chain drops role phrases; survivors list is empty.
    assert [r.text for r in result] == []


@pytest.mark.unit
def test_real_org_passes_through_default_chain() -> None:
    """A bona-fide org name that NER caught should survive the default chain."""
    nlp = _FakeNlp(_doc_with_entities("Bupa Australia announced a new initiative.", [("Bupa", "ORG")]))
    neo4j = FakeNeo4jClient(entities=[])

    result = suggest_entities("Bupa Australia announced a new initiative.", neo4j, nlp=nlp)

    assert [r.text for r in result] == ["Bupa"]
    assert result[0].label == "ORG"
    assert result[0].is_new is True


@pytest.mark.unit
def test_allowlist_promotes_missing_org() -> None:
    """Avanade isn't in the en_core_web_sm vocabulary; allowlist promotes it."""
    # NER finds nothing relevant; the chain promotes Avanade because it's in the allowlist
    # and substring-matches the input text.
    nlp = _FakeNlp(_doc_with_entities("Avanade is a Microsoft partner.", []))
    neo4j = FakeNeo4jClient(entities=[])

    chain = ChainedSuggestionFilter(
        filters=[
            RolePhraseFilter(),
            KnownEntityAllowlist(entities=[{"text": "Avanade", "label": "ORG"}]),
            NerLabelFilter(person_overrides=set(), org_overrides=set()),
        ]
    )

    result = suggest_entities(
        "Avanade is a Microsoft partner.", neo4j, nlp=nlp, filter_chain=chain
    )

    assert "Avanade" in [r.text for r in result]
    avanade = next(r for r in result if r.text == "Avanade")
    assert avanade.label == "ORG"
    # Allowlist promotions get no NER context sentence
    assert avanade.context == ""


@pytest.mark.unit
def test_label_override_corrects_mistype() -> None:
    """NER tagged 'Mitch Tomazic' as ORG; NerLabelFilter overrides to PERSON."""
    nlp = _FakeNlp(_doc_with_entities("Mitch Tomazic joined the team.", [("Mitch Tomazic", "ORG")]))
    neo4j = FakeNeo4jClient(entities=[])

    chain = ChainedSuggestionFilter(
        filters=[
            RolePhraseFilter(),
            KnownEntityAllowlist(entities=[]),
            NerLabelFilter(person_overrides={"Mitch Tomazic"}, org_overrides=set()),
        ]
    )

    result = suggest_entities(
        "Mitch Tomazic joined the team.", neo4j, nlp=nlp, filter_chain=chain
    )

    assert len(result) == 1
    assert result[0].text == "Mitch Tomazic"
    assert result[0].label == "PERSON"


@pytest.mark.unit
def test_neo4j_unavailable_short_circuits_before_filter() -> None:
    """When Neo4j is down, the function returns [] without invoking nlp or filter."""

    class _UnavailableNeo4j:
        available = False

    nlp = _FakeNlp(_doc_with_entities("Bupa announced.", [("Bupa", "ORG")]))
    result = suggest_entities("Bupa announced.", _UnavailableNeo4j(), nlp=nlp)
    assert result == []
