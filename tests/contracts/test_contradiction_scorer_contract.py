"""Contract tests for ContradictionScorer + ClaimExtractor Protocols."""

from __future__ import annotations

import pytest

from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor
from kairix.knowledge.contradict.protocols import ClaimExtractor, ContradictionScorer
from kairix.knowledge.contradict.scorers import (
    CompositeContradictionScorer,
    DirectContradictionScorer,
    OverstatementScorer,
    StatusMismatchScorer,
)


class _FakeLLM:
    def chat(self, messages: list[dict]) -> str:
        return '{"score": 0.0, "reason": ""}'


@pytest.mark.contract
def test_direct_scorer_satisfies_protocol() -> None:
    assert isinstance(DirectContradictionScorer(_FakeLLM()), ContradictionScorer)


@pytest.mark.contract
def test_overstatement_scorer_satisfies_protocol() -> None:
    assert isinstance(OverstatementScorer(_FakeLLM()), ContradictionScorer)


@pytest.mark.contract
def test_status_mismatch_scorer_satisfies_protocol() -> None:
    assert isinstance(StatusMismatchScorer(_FakeLLM()), ContradictionScorer)


@pytest.mark.contract
def test_composite_scorer_satisfies_protocol() -> None:
    composite = CompositeContradictionScorer(scorers=[DirectContradictionScorer(_FakeLLM())])
    assert isinstance(composite, ContradictionScorer)


@pytest.mark.contract
def test_entity_density_extractor_satisfies_protocol() -> None:
    assert isinstance(EntityDensityClaimExtractor(), ClaimExtractor)


@pytest.mark.contract
def test_in_test_scorer_fake_satisfies_protocol() -> None:
    """Tests can build their own scorer fake — must structurally satisfy the protocol."""

    class _FakeScorer:
        category = "fake"

        def score(self, claim: str, candidate: str) -> tuple[float, str]:
            return 0.5, "fake"

    assert isinstance(_FakeScorer(), ContradictionScorer)
