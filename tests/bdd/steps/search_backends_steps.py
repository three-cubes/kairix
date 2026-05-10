"""Step definitions for search_backends.feature.

Exercises ``BM25SearchBackend`` (the SearchBackend adapter from
``kairix.core.search.backends``) against the canonical
``FakeDocumentRepository`` from ``tests/fakes.py`` — no monkeypatching, no
``@patch``. The fake satisfies the ``DocumentRepository`` Protocol from
``kairix.core.protocols``.
"""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend
from tests.fakes import FakeDocumentRepository

# Module-level state, scoped to a single scenario by pytest-bdd.
_state: dict = {}


@given("a document repository indexed with kairix architecture and runbook docs")
def doc_repo_with_seeded_docs() -> None:
    """Seed FakeDocumentRepository with three shared docs and one alpha-collection doc."""
    docs = [
        {
            "path": "vault/architecture.md",
            "title": "Architecture",
            "content": "kairix architecture patterns",
            "collection": "shared",
        },
        {
            "path": "vault/runbook.md",
            "title": "Runbook",
            "content": "operational runbook for restart",
            "collection": "shared",
        },
        {
            "path": "vault/recipes.md",
            "title": "Recipes",
            "content": "unrelated cooking recipes",
            "collection": "shared",
        },
        {
            "path": "vault/agent-alpha-notes.md",
            "title": "Alpha Notes",
            "content": "alpha-only notes about kairix",
            "collection": "alpha",
        },
    ]
    _state.clear()
    _state["backend"] = BM25SearchBackend(FakeDocumentRepository(documents=docs))


@when(parsers.parse('I search the BM25 backend for "{query}"'))
def search_bm25(query: str) -> None:
    backend: BM25SearchBackend = _state["backend"]
    _state["results"] = backend.search(query)


@when(parsers.parse('I search the BM25 backend for "{query}" restricted to collection "{collection}"'))
def search_bm25_with_collection(query: str, collection: str) -> None:
    backend: BM25SearchBackend = _state["backend"]
    _state["results"] = backend.search(query, collections=[collection])


@then(parsers.parse("the BM25 backend returns {count:d} result"))
@then(parsers.parse("the BM25 backend returns {count:d} results"))
def bm25_result_count(count: int) -> None:
    results = _state["results"]
    assert len(results) == count, (
        f"Expected {count} BM25 results, got {len(results)}: paths={[r.get('path') for r in results]}"
    )


@then(parsers.parse('the BM25 result paths include "{expected_path}"'))
def bm25_result_includes(expected_path: str) -> None:
    paths = [r.get("path") for r in _state["results"]]
    assert expected_path in paths, f"{expected_path!r} not in {paths!r}"


@then(parsers.parse('the BM25 result paths exclude "{forbidden_path}"'))
def bm25_result_excludes(forbidden_path: str) -> None:
    paths = [r.get("path") for r in _state["results"]]
    assert forbidden_path not in paths, f"{forbidden_path!r} unexpectedly in {paths!r}"
