"""
Tests for kairix.core.search.intent — query intent classifier.

Labelled examples covering all six intent classes (TEMPORAL, MULTI_HOP, ENTITY,
PROCEDURAL, KEYWORD, SEMANTIC).
Priority order tested: TEMPORAL > MULTI_HOP > ENTITY > PROCEDURAL > KEYWORD > SEMANTIC.
"""

import pytest

from kairix.core.search.intent import QueryIntent, classify

# ---------------------------------------------------------------------------
# Labelled test cases
# ---------------------------------------------------------------------------

CASES: list[tuple[str, QueryIntent]] = [
    # --- TEMPORAL (6 cases) ---
    ("what was completed last week", QueryIntent.TEMPORAL),
    ("what changed in March", QueryIntent.TEMPORAL),
    ("when did we fix the embed lock crash", QueryIntent.TEMPORAL),
    ("what has been done recently", QueryIntent.TEMPORAL),
    ("show me items completed on 2026-03-22", QueryIntent.TEMPORAL),
    ("what happened over the last 30 days", QueryIntent.TEMPORAL),
    # --- MULTI_HOP (3 cases) ---
    ("how does the embed loop relate to the budget trim", QueryIntent.MULTI_HOP),
    ("explain the tradeoffs between BM25 and vector retrieval", QueryIntent.MULTI_HOP),
    ("what is the connection between intent classification and rerank cost", QueryIntent.MULTI_HOP),
    # --- ENTITY (4 cases) ---
    ("tell me about Jordan Blake", QueryIntent.ENTITY),
    ("what has Builder done", QueryIntent.ENTITY),
    ("what do we know about BuilderCo", QueryIntent.ENTITY),
    ("who is Jordan Blake", QueryIntent.ENTITY),
    # --- PROCEDURAL (4 cases) ---
    ("how to fetch a Key Vault secret", QueryIntent.PROCEDURAL),
    ("how do I handle a merge conflict", QueryIntent.PROCEDURAL),
    ("what's the rule for writing credentials", QueryIntent.PROCEDURAL),
    ("should I use trash instead of rm", QueryIntent.PROCEDURAL),
    # --- KEYWORD (3 cases) ---
    ("SQLiteVec Extension", QueryIntent.KEYWORD),
    ("SchemaVersionError", QueryIntent.KEYWORD),
    ("/data/workspaces/builder/MEMORY.md", QueryIntent.KEYWORD),
    # --- SEMANTIC (3 cases) ---
    ("why does hybrid search outperform pure vector", QueryIntent.SEMANTIC),
    ("explain the architecture of the kairix memory system", QueryIntent.SEMANTIC),
    ("what are the trade-offs between BM25 and vector search", QueryIntent.SEMANTIC),
]


@pytest.mark.unit
@pytest.mark.parametrize("query,expected", CASES)
def test_intent_labelled_examples(query: str, expected: QueryIntent) -> None:
    """Each labelled query must be classified to the expected intent."""
    result = classify(query)
    assert result == expected, f"Query: {query!r}\n  Expected: {expected.value}\n  Got:      {result.value}"


# ---------------------------------------------------------------------------
# Contract tests — boundary conditions and never-raise guarantee
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_classify_empty_string() -> None:
    """Empty string returns SEMANTIC (default)."""
    assert classify("") == QueryIntent.SEMANTIC


@pytest.mark.contract
def test_classify_whitespace_only() -> None:
    """Whitespace-only returns SEMANTIC."""
    assert classify("   ") == QueryIntent.SEMANTIC


@pytest.mark.contract
def test_classify_never_raises() -> None:
    """Classifier must never raise even for garbage input."""
    garbage_inputs = [
        None,  # type: ignore[arg-type]
        12345,  # type: ignore[arg-type]
        "!@#$%^&*()",
        "\x00\x01\x02",
        "a" * 10_000,
    ]
    for inp in garbage_inputs:
        result = classify(inp)  # type: ignore[arg-type]
        assert isinstance(result, QueryIntent)


@pytest.mark.unit
def test_classify_returns_query_intent_enum() -> None:
    """Return value is always a QueryIntent member."""
    for query, _ in CASES:
        result = classify(query)
        assert result in list(QueryIntent)


@pytest.mark.unit
def test_temporal_beats_entity() -> None:
    """TEMPORAL takes priority over ENTITY signals in the same query."""
    # "tell me about" is ENTITY but "recently" is TEMPORAL → TEMPORAL wins
    result = classify("tell me about what BuilderCo did recently")
    assert result == QueryIntent.TEMPORAL


@pytest.mark.unit
def test_temporal_beats_multi_hop() -> None:
    """TEMPORAL takes priority over MULTI_HOP signals in the same query.
    Per docstring priority: TEMPORAL > MULTI_HOP.
    """
    # "tradeoffs" is MULTI_HOP, "last week" is TEMPORAL → TEMPORAL wins.
    result = classify("what tradeoffs did we discuss last week")
    assert result == QueryIntent.TEMPORAL


@pytest.mark.unit
def test_multi_hop_beats_entity() -> None:
    """MULTI_HOP takes priority over ENTITY in the same query.
    Per docstring priority: MULTI_HOP > ENTITY.
    """
    # "tell me about" is ENTITY, "tradeoffs" is MULTI_HOP → MULTI_HOP wins.
    result = classify("tell me about the tradeoffs in our retrieval pipeline")
    assert result == QueryIntent.MULTI_HOP


@pytest.mark.unit
def test_entity_beats_procedural() -> None:
    """ENTITY takes priority over PROCEDURAL."""
    result = classify("what do we know about how to write rules")
    # "what do we know about" → ENTITY; "how to" → PROCEDURAL → ENTITY wins
    assert result == QueryIntent.ENTITY


@pytest.mark.unit
def test_procedural_beats_keyword() -> None:
    """PROCEDURAL takes priority over KEYWORD signals."""
    result = classify("how do I use SQLiteVec Extension")
    assert result == QueryIntent.PROCEDURAL


@pytest.mark.unit
def test_version_string_is_keyword() -> None:
    """Queries containing version strings are KEYWORD."""
    assert classify("kairix v1.1.2 changelog") == QueryIntent.KEYWORD


@pytest.mark.unit
def test_http_error_code_is_keyword() -> None:
    """HTTP 4xx/5xx codes are KEYWORD."""
    assert classify("why am I getting 429 errors") == QueryIntent.KEYWORD


@pytest.mark.unit
def test_allcaps_error_code_is_keyword() -> None:
    """ALLCAPS error codes are KEYWORD."""
    assert classify("AZURE-OPENAI-001 error diagnosis") == QueryIntent.KEYWORD
