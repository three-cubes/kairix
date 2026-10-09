"""Contract: intent classifier contract tests.

The intent classifier is the most critical contract in the search domain:
it must never raise, must always return a valid QueryIntent, and must
route the canonical query patterns to their expected intents.
"""

import pytest

from kairix.core.search.intent import QueryIntent, classify

_CANONICAL_CASES = [
    ("what happened last week", QueryIntent.TEMPORAL),
    ("what did we complete yesterday", QueryIntent.TEMPORAL),
    ("tell me about OpenClaw", QueryIntent.ENTITY),
    ("who is Alice Smith", QueryIntent.ENTITY),
    ("how do I run the embedding pipeline", QueryIntent.PROCEDURAL),
    ("steps to restart the service", QueryIntent.PROCEDURAL),
    ("FEAT-081 implementation status", QueryIntent.KEYWORD),
    ("infrastructure cost optimisation strategy", QueryIntent.SEMANTIC),
    ("connection between OpenClaw and Acme Partners", QueryIntent.MULTI_HOP),
]


@pytest.mark.contract
@pytest.mark.parametrize("query,expected", _CANONICAL_CASES)
def test_intent_canonical_routing(query, expected):
    """Canonical queries must route to their expected intent."""
    result = classify(query)
    assert result == expected, f"Query {query!r}: expected {expected}, got {result}"


@pytest.mark.contract
def test_intent_never_raises_on_empty():
    result = classify("")
    assert isinstance(result, QueryIntent)


@pytest.mark.contract
def test_intent_never_raises_on_garbage():
    result = classify("!@#$%^&*()")
    assert isinstance(result, QueryIntent)


@pytest.mark.contract
def test_intent_never_raises_on_very_long_input():
    result = classify("word " * 500)
    assert isinstance(result, QueryIntent)


@pytest.mark.contract
def test_intent_returns_valid_enum_member():
    for query, _ in _CANONICAL_CASES:
        result = classify(query)
        assert result in QueryIntent


@pytest.mark.contract
def test_temporal_beats_entity():
    """Temporal intent has higher priority than entity."""
    result = classify("what did OpenClaw do last week")
    assert result == QueryIntent.TEMPORAL


@pytest.mark.contract
def test_multi_hop_beats_entity():
    """Multi-hop intent has higher priority than entity."""
    result = classify("connection between Alice Smith and Acme Partners")
    assert result == QueryIntent.MULTI_HOP
