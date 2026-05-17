"""Adapter classes implementing SearchBackend for BM25 and vector search.

Each backend wraps a protocol implementation (DocumentRepository, VectorRepository,
EmbeddingService) behind a uniform search(query, collections, limit) interface.

These adapters are composed into SearchPipeline — callers never construct them
directly in production code.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairix.core.protocols import (
        DocumentRepository,
        EmbeddingService,
        VectorRepository,
    )

logger = logging.getLogger(__name__)


class BM25SearchBackend:
    """SearchBackend adapter wrapping BM25 full-text search via DocumentRepository."""

    def __init__(self, doc_repo: DocumentRepository) -> None:
        self._doc_repo = doc_repo

    def search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Delegate to DocumentRepository.search_fts."""
        return self._doc_repo.search_fts(query, collections=collections, limit=limit)

    def get_chunk_dates(self, paths: list[str]) -> dict[str, str]:
        """Proxy DocumentRepository.get_chunk_dates so the pipeline can enrich
        FusedResults with chunk_date metadata before the boost chain runs.

        Returns ``{path: chunk_date_iso}`` for paths that carry a chunk_date;
        absent paths are simply not in the dict. Returns ``{}`` on repo failure.
        """
        try:
            return self._doc_repo.get_chunk_dates(paths)
        except Exception as e:
            logger.warning("BM25SearchBackend.get_chunk_dates: %s", e)
            return {}


class VectorSearchBackend:
    """SearchBackend adapter wrapping vector search with optional HyDE.

    Embeds the query text via EmbeddingService and searches the VectorRepository.
    When an LLM backend is provided, can apply HyDE (Hypothetical Document
    Embeddings) for semantic/multi_hop intents — not implemented in Phase 4.
    """

    def __init__(
        self,
        embedding: EmbeddingService,
        vector_repo: VectorRepository,
        llm: object | None = None,
    ) -> None:
        self._embedding = embedding
        self._vector_repo = vector_repo
        self._llm = llm  # For HyDE — optional LLMBackend

    def search(
        self,
        query: str,
        collections: list[str] | None = None,
        limit: int = 10,
        *,
        timings: dict[str, float] | None = None,
    ) -> list[dict]:
        """Embed query and run ANN vector search.

        Propagates exceptions to the caller — symmetrical with
        ``BM25SearchBackend.search``. The pipeline's outer try/except catches
        them and sets ``vec_failed=True``. An empty result list is a
        successful no-match (``vec_failed=False``); a raised exception is a
        genuine backend failure (``vec_failed=True``). Conflating empty with
        failed produced false-positive operator alerts before this change.

        An empty embedding from the EmbeddingService is treated as a failure
        (raised, not returned as ``[]``) so operators see vec_failed=True
        when the embedding pipeline is broken.

        When ``timings`` is supplied, records the ``embed_http`` (embed-call
        wall-clock) and ``vector_ann`` (vector-repo search wall-clock) deltas
        into it in milliseconds. Lets ``SearchPipeline._dispatch_vector``
        decompose the ``vector`` stage so probe data can attribute slow tail
        queries to Azure HTTP tail vs local ANN cost (#282 follow-up).
        """
        t = time.monotonic()
        vec = self._embedding.embed(query)
        embed_ms = (time.monotonic() - t) * 1000.0
        if timings is not None:
            timings["embed_http"] = round(embed_ms, 2)
        if not vec:
            raise RuntimeError("VectorSearchBackend: embedding service returned no vector")
        t = time.monotonic()
        try:
            return self._vector_repo.search(vec, k=limit, collections=collections)
        finally:
            if timings is not None:
                timings["vector_ann"] = round((time.monotonic() - t) * 1000.0, 2)


class AzureEmbeddingService:
    """EmbeddingService adapter wrapping kairix._azure embed functions.

    DEPRECATED in v2026.5.17 — kept only as a transitional shim while
    callers migrate to ``ProviderEmbeddingService`` (lives in
    ``kairix.transport.embed_service``). New code (and every production
    factory wire) constructs
    ``ProviderEmbeddingService(get_provider(provider_name()))`` instead.
    This class is removed once every internal caller has migrated.

    Lazily imports kairix._azure to avoid hard dependency at module load.
    Uses existing credential resolution from the Azure module.
    """

    def __init__(self) -> None:
        pass  # Uses existing credential resolution

    def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns [] on failure."""
        from kairix._azure import embed_text

        return embed_text(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts sequentially. Returns list of vectors."""
        from kairix._azure import embed_text

        return [embed_text(t) for t in texts]
