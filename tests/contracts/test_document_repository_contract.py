"""Contract tests for DocumentRepository fakes.

The production ``BM25Result`` TypedDict declares the FTS-search row shape
as ``{file, title, snippet, score, collection}``. Any fake that claims
to satisfy the ``DocumentRepository.search_fts`` Protocol must emit
results conforming to that shape — otherwise integration tests that
wire the fake → ``RRFFusion`` silently produce no results (RRF reads
``result["file"]`` and current rrf catches the KeyError into ``[]``).

This is the test that #162 was about: the fake was emitting ``path``
keys, RRF swallowed the KeyError, and existing integration tests
"passed" against a no-op fusion.
"""

from __future__ import annotations

import pytest

from kairix.core.search.bm25 import BM25Result
from tests.fakes import FakeDocumentRepository


@pytest.mark.contract
def test_search_fts_emits_bm25_result_shape_with_file_key() -> None:
    """The fake must emit dicts with the ``file`` key — that's what
    ``BM25Result`` and downstream ``RRFFusion`` consume. A doc keyed only
    on ``path`` would silently disappear in fusion (KeyError swallowed).
    """
    repo = FakeDocumentRepository(
        documents=[
            {
                "path": "vault/notes/foo.md",
                "title": "Foo",
                "content": "the term we will search for",
                "collection": "vault",
            }
        ]
    )
    rows = repo.search_fts("search for", limit=10)
    assert rows, "expected at least one match for the documented term"
    row = rows[0]
    # The required keys per BM25Result TypedDict — anything else means
    # the fake is incompatible with downstream BM25Result-consuming code.
    for required in BM25Result.__required_keys__:
        assert required in row, (
            f"FakeDocumentRepository.search_fts emitted a row missing the BM25Result key {required!r}: {row!r}"
        )


@pytest.mark.contract
def test_search_fts_force_rows_passes_through_with_file_key_intact() -> None:
    """Scripted-mode rows are returned verbatim; if the caller supplies
    BM25Result-shaped rows they must arrive intact.
    """
    canned = [{"file": "a.md", "title": "A", "snippet": "s", "score": 0.5, "collection": "vault"}]
    repo = FakeDocumentRepository(force_rows=canned)
    rows = repo.search_fts("anything")
    assert rows == canned
