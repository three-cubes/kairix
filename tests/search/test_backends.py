"""Unit tests for ``kairix.core.search.backends``.

Integration coverage lives in ``tests/integration/test_backends_integration.py``.
These unit tests pin the defensive branches not exercised by the
integration suite — specifically the never-raises contract on
``BM25SearchBackend.get_chunk_dates`` and the batch path on
``AzureEmbeddingService.embed_batch``.

Every test sabotage-proofs the line under test (mutate the production
branch → run → confirm the test fails → restore).
"""

from __future__ import annotations

import pytest

from kairix.core.search.backends import (
    AzureEmbeddingService,
    BM25SearchBackend,
)
from tests.fakes import FakeDocumentRepository

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# BM25SearchBackend.get_chunk_dates — never-raises contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_get_chunk_dates_returns_empty_dict_when_repo_raises() -> None:
    # Sabotage: removing the `except Exception: return {}` block makes
    # the test propagate the RuntimeError and the call() expression raises
    # instead of returning {}.
    class _RaisingRepo:
        def search_fts(self, *_args, **_kwargs):  # pragma: no cover - unused
            return []

        def get_chunk_dates(self, _paths):
            raise RuntimeError("simulated repo failure")

    backend = BM25SearchBackend(_RaisingRepo())  # type: ignore[arg-type]  # structural duck-typing for the test
    out = backend.get_chunk_dates(["/vault/a.md", "/vault/b.md"])
    assert out == {}


@pytest.mark.unit
def test_bm25_get_chunk_dates_passes_through_repo_values() -> None:
    # Sabotage: swapping the return-from-repo for `return {}` makes the
    # length assert fail — pins that the happy path is not the fallback.
    repo = FakeDocumentRepository(
        documents=[
            {
                "path": "/vault/a.md",
                "collection": "c",
                "title": "A",
                "content": "x",
                "chunk_date": "2026-05-10",
            },
            {
                "path": "/vault/b.md",
                "collection": "c",
                "title": "B",
                "content": "y",
                "chunk_date": "2026-05-11",
            },
        ],
    )
    backend = BM25SearchBackend(repo)  # type: ignore[arg-type]  # FakeDocumentRepository satisfies the protocol structurally
    out = backend.get_chunk_dates(["/vault/a.md", "/vault/b.md"])
    assert out == {"/vault/a.md": "2026-05-10", "/vault/b.md": "2026-05-11"}


# ---------------------------------------------------------------------------
# AzureEmbeddingService.embed_batch — batch path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_azure_embed_batch_returns_one_vector_per_input(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Sabotage: changing `[embed_text(t) for t in texts]` to `[embed_text(texts[0])]`
    # makes len(out) == 1, not 3, and the test fails.
    #
    # The production embed_text returns [] when Azure creds aren't present
    # (logged to stderr). That's the graceful-degrade contract.
    service = AzureEmbeddingService()
    out = service.embed_batch(["one", "two", "three"])
    # One result per input, no exception even when creds are missing.
    assert len(out) == 3
    # Each element is a list (empty when creds missing, non-empty in prod).
    assert all(isinstance(vec, list) for vec in out)


@pytest.mark.unit
def test_azure_embed_batch_empty_input_returns_empty_list() -> None:
    # Sabotage: changing the comprehension to `[embed_text("placeholder")]`
    # makes the empty-input case return one element instead of zero.
    service = AzureEmbeddingService()
    out = service.embed_batch([])
    assert out == []
