"""Unit tests for EntityDensityClaimExtractor."""

from __future__ import annotations

import pytest

from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor


@pytest.mark.unit
def test_empty_content_returns_empty_list() -> None:
    extractor = EntityDensityClaimExtractor()
    assert extractor.extract("", top_n=3) == []
    assert extractor.extract("   ", top_n=3) == []


@pytest.mark.unit
def test_short_content_returns_single_claim() -> None:
    """Content without sentence boundaries comes through as one claim."""
    extractor = EntityDensityClaimExtractor()
    result = extractor.extract("AcmeCorp is the only provider", top_n=3)
    assert len(result) == 1
    assert "AcmeCorp" in result[0]


@pytest.mark.unit
def test_ranks_high_entity_density_sentences_first() -> None:
    """Sentences mentioning more proper nouns rank above prose."""
    content = (
        "This is some background. "
        "AcmeCorp acquired BetaTech and TestInc in Sydney last quarter. "
        "Things were generally fine."
    )
    extractor = EntityDensityClaimExtractor()
    result = extractor.extract(content, top_n=1)
    assert len(result) == 1
    assert "AcmeCorp" in result[0]
    assert "BetaTech" in result[0]


@pytest.mark.unit
def test_modal_words_boost_score() -> None:
    """A sentence with 'only' / 'monopoly' outranks a similar-length factual sentence
    when entity density is otherwise comparable."""
    content = "AcmeCorp is the only provider of Widgets. AcmeCorp also makes Widgets in Sydney."
    extractor = EntityDensityClaimExtractor()
    # Both sentences have AcmeCorp and Widgets; the first has "only" (modal-weighted)
    result = extractor.extract(content, top_n=1)
    assert len(result) == 1
    assert "only" in result[0]


@pytest.mark.unit
def test_top_n_truncates_results() -> None:
    content = "A is X. B is Y. C is Z. D is W. E is V."
    extractor = EntityDensityClaimExtractor()
    result = extractor.extract(content, top_n=2)
    assert len(result) == 2


@pytest.mark.unit
def test_top_n_larger_than_sentence_count_returns_all() -> None:
    content = "A is X. B is Y."
    extractor = EntityDensityClaimExtractor()
    result = extractor.extract(content, top_n=10)
    assert len(result) == 2
