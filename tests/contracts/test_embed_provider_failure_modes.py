"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EmbedProvider`.

``EmbedProvider.embed_batch(texts, *, model, dims)`` is the SDK-backed
embedding seam the post-embed recall gate (:class:`RecallChecker`) uses
to embed its canary queries. When the endpoint is unreachable the SDK
raises; the recall gate must record each query as *skipped* (not as a
miss, and never as a crash) so a provider outage is distinguishable from
an index regression.

One body, two implementations (F43 limb 2):

* the real :class:`OpenAIEmbedProvider`, pointed at an endpoint whose port
  is out of range so the openai SDK's transport fails address resolution
  locally (no socket is opened, ``max_retries=0`` so there is no backoff);
* the canonical :class:`tests.fakes.FakeEmbedProvider` with ``raises=``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.embed.recall_check import RecallChecker
from kairix.platform.llm.embed_provider import OpenAIEmbedProvider
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

pytestmark = pytest.mark.contract

# Port 99999 is outside the TCP range: address resolution fails before any
# connection attempt, so the real SDK raises APIConnectionError hermetically.
_UNREACHABLE_ENDPOINT = "http://127.0.0.1:99999/v1"

_QUERIES = [
    ("R01", "architecture decision record", "architecture"),
    ("R02", "how to deploy", "deploy"),
]

_FACTORIES: list[Callable[[], Any]] = [
    lambda: OpenAIEmbedProvider(api_key="sk-f68-contract", endpoint=_UNREACHABLE_ENDPOINT, max_retries=0),
    lambda: FakeEmbedProvider(raises=ConnectionError("F68-embed-endpoint-unreachable")),
]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-openai-sdk", "fake"])
def test_embed_batch_unavailable_endpoint_skips_every_recall_query(factory: Callable[[], Any]) -> None:
    """An unreachable embed endpoint marks every recall query ``skipped``.

    The vector searcher would have returned a hit for every query; because
    embedding failed, it is never consulted and the gate reports
    ``total == 0`` rather than a misleading 100% (or 0%) recall.

    Sabotage proof: in ``kairix/core/embed/recall_check.py::_embed_query``
    replace the ``except Exception`` body's ``return None`` with ``raise``.
    Re-run: both cases fail because ``RecallChecker.check`` propagates the
    provider error instead of skipping the query. Restored.
    """
    provider = factory()
    searcher = FakeVectorSearcher(paths=["architecture/adr-001.md", "ops/how-to-deploy.md"])
    checker = RecallChecker(embed_provider=provider, vector_searcher=searcher)
    db = sqlite3.connect(":memory:")
    try:
        result = checker.check(db=db, recall_queries=_QUERIES, canary_cache_path=None)
    finally:
        db.close()

    assert [d["skipped"] for d in result["detail"]] == [True, True]
    assert result["total"] == 0
    assert result["passed"] == 0
    assert searcher.calls == []
