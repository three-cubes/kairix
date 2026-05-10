"""Coverage tests for the alarm-system failure paths in recall_check.

The ``recall_check`` module is the post-embed quality gate. Its happy
paths are covered by ``test_recall_check.py`` and
``test_recall_check_contracts.py``; this file targets the failure paths
that used to be ``# pragma: no cover``-marked because no test could
reach them through the public surface:

  - credentials missing / empty / wrong type → query is skipped
  - ``provider_factory`` raises → query is skipped
  - usearch index missing / search raises → empty results, no crash
  - lazy production defaults fire on first call when no overrides given

Every test drives behaviour through the public ``RecallChecker.check``
or the ``UsearchVectorSearcher`` constructor — no internal-name
imports of helper functions, no ``@patch``, no ``monkeypatch.setenv``.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from kairix.core.embed.recall_check import RecallChecker, UsearchVectorSearcher
from tests.fakes import FakeCredentials, FakeEmbedProvider, FakeVectorSearcher

# ---------------------------------------------------------------------------
# Credentials-resolution failure paths — driven via ``creds_resolver`` /
# ``provider_factory`` constructor injection. Every query that fails to
# resolve a usable provider must surface as ``skipped=True`` in the
# returned detail, NOT as a crash.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_skips_when_creds_resolver_returns_none() -> None:
    """``creds_resolver`` returning ``None`` → no provider can be built → query is skipped."""

    def _no_creds() -> None:
        return None

    checker = RecallChecker(
        # No embed_provider — forces the lazy resolution path.
        vector_searcher=FakeVectorSearcher(paths=["docs/whatever.md"]),
        creds_resolver=_no_creds,
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["total"] == 0
    assert result["passed"] == 0
    assert result["score"] == pytest.approx(0.0)
    assert result["detail"][0]["skipped"] is True


@pytest.mark.unit
def test_check_skips_when_creds_resolver_returns_non_credentials_type() -> None:
    """``creds_resolver`` returning something other than ``Credentials``
    (e.g. ``GraphCredentials``) is treated as no-creds → query is skipped.

    This protects against config wiring that accidentally points the embed
    gate at the graph creds — without this guard the gate would crash with
    AttributeError on ``creds.api_key`` later.
    """
    from kairix.credentials import GraphCredentials

    def _wrong_kind() -> GraphCredentials:
        return GraphCredentials(uri="bolt://x", user="u", password="p")

    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=_wrong_kind,
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["detail"][0]["skipped"] is True
    assert result["total"] == 0


@pytest.mark.unit
def test_check_skips_when_credentials_have_empty_api_key() -> None:
    """A ``Credentials`` whose ``api_key`` is empty is unusable → skip the query.

    Empty strings can leak through env-var resolution (``KAIRIX_*=""``); the
    gate must short-circuit on the credentials check itself, BEFORE the
    provider factory runs. Verified by injecting a factory that would
    succeed if called — the test passes only because the factory is never
    reached for empty-api-key creds.
    """
    factory_calls: list[object] = []
    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=lambda: FakeCredentials(api_key=""),
        provider_factory=lambda c: factory_calls.append(c) or FakeEmbedProvider(),  # type: ignore[func-returns-value, arg-type, return-value]  # one-shot capture-and-return
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["detail"][0]["skipped"] is True
    # Critical: the factory must NOT have been called for empty-api-key creds.
    assert factory_calls == []


@pytest.mark.unit
def test_check_skips_when_credentials_have_empty_endpoint() -> None:
    """A ``Credentials`` whose ``endpoint`` is empty is unusable → skip the query.

    Verified that the credentials check runs BEFORE the provider factory:
    we inject a factory that would succeed and assert it was never called.
    """
    factory_calls: list[object] = []
    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=lambda: FakeCredentials(endpoint=""),
        provider_factory=lambda c: factory_calls.append(c) or FakeEmbedProvider(),  # type: ignore[func-returns-value, arg-type, return-value]  # one-shot capture-and-return
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["detail"][0]["skipped"] is True
    assert factory_calls == []


@pytest.mark.unit
def test_check_skips_when_provider_factory_raises() -> None:
    """``provider_factory`` raising (e.g. SDK init failure) → skip the query, do not propagate."""
    creds_calls: list[int] = []

    def _creds() -> object:
        creds_calls.append(1)
        return FakeCredentials()

    def _exploding_factory(creds: object) -> object:
        raise RuntimeError("openai SDK init failed")

    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=_creds,
        provider_factory=_exploding_factory,
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["detail"][0]["skipped"] is True
    assert result["total"] == 0
    # creds_resolver was called exactly once before the factory exploded.
    assert len(creds_calls) == 1


@pytest.mark.unit
def test_check_uses_provider_factory_when_credentials_resolve_successfully() -> None:
    """Happy lazy-resolution path: creds resolve → factory returns a provider →
    the resolved provider is reused on subsequent calls (no re-resolution per query).
    """
    factory_calls: list[object] = []
    factory_provider = FakeEmbedProvider(vector=[1.0, 0.0, 0.0])

    def _factory(creds: object) -> object:
        factory_calls.append(creds)
        return factory_provider

    fake_searcher = FakeVectorSearcher(paths=["docs/architecture.md"])
    checker = RecallChecker(
        vector_searcher=fake_searcher,
        creds_resolver=lambda: FakeCredentials(),
        provider_factory=_factory,
    )
    result = checker.check(
        recall_queries=[
            ("R1", "q1", "architecture"),
            ("R2", "q2", "architecture"),
        ]
    )

    # Both queries hit (gold fragment "architecture" is in the returned path).
    assert result["passed"] == 2
    assert result["total"] == 2
    # The factory was called exactly once across two queries — provider is cached.
    assert len(factory_calls) == 1
    # And the provider itself saw both queries.
    assert len(factory_provider.calls) == 2


@pytest.mark.unit
def test_check_uses_credentials_model_when_no_explicit_model() -> None:
    """When the resolved Credentials carry a ``model``, the recall gate
    passes that model name through to ``provider.embed_batch`` rather than
    falling back to the hardcoded default.
    """
    factory_provider = FakeEmbedProvider(vector=[1.0, 0.0, 0.0])

    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(paths=["docs/x.md"]),
        creds_resolver=lambda: FakeCredentials(model="custom-embed-model-v9"),
        provider_factory=lambda _creds: factory_provider,
    )
    checker.check(recall_queries=[("R1", "q", "x")])

    assert len(factory_provider.calls) == 1
    assert factory_provider.calls[0]["model"] == "custom-embed-model-v9"


@pytest.mark.unit
def test_check_falls_back_to_default_model_when_credentials_have_empty_model() -> None:
    """``creds.model`` is the empty string → the gate falls back to
    ``text-embedding-3-large`` (the documented default).
    """
    factory_provider = FakeEmbedProvider(vector=[1.0, 0.0, 0.0])

    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(paths=["docs/x.md"]),
        creds_resolver=lambda: FakeCredentials(model=""),
        provider_factory=lambda _creds: factory_provider,
    )
    checker.check(recall_queries=[("R1", "q", "x")])

    assert factory_provider.calls[0]["model"] == "text-embedding-3-large"


# ---------------------------------------------------------------------------
# UsearchVectorSearcher — drive every branch via the ``index_resolver``
# constructor seam. No ``# pragma: no cover``; the production wrapper is
# now testable.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_usearch_searcher_returns_empty_list_when_index_resolver_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """A missing usearch index (``get_vector_index() is None``) → empty list,
    NOT an exception. The wrapper logs the dedicated "index not available"
    warning rather than the generic "search failed" — this distinguishes
    the early-return branch from the catch-all error path.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="kairix.core.embed.recall_check")
    searcher = UsearchVectorSearcher(index_resolver=lambda: None)
    result = searcher.search_vectors(np.array([0.1, 0.2, 0.3], dtype=np.float32), limit=5)
    assert result == []
    # Dedicated "index missing" warning fired — NOT the generic catch-all path.
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("usearch index not available" in m for m in messages), (
        f"expected 'index not available' warning; got {messages}"
    )
    assert not any("recall search failed" in m for m in messages), (
        f"early-return branch should not log generic 'search failed'; got {messages}"
    )


@pytest.mark.unit
def test_usearch_searcher_returns_paths_from_index_search_results() -> None:
    """The wrapper extracts the ``path`` field from each search hit and
    returns them in order.
    """

    class _StubIndex:
        def __init__(self) -> None:
            self.search_calls: list[tuple[Any, int]] = []

        def search(self, vector: Any, *, k: int) -> list[dict[str, str]]:
            self.search_calls.append((vector, k))
            return [
                {"path": "docs/a.md", "score": 0.9},
                {"path": "docs/b.md", "score": 0.8},
            ]

    index = _StubIndex()
    searcher = UsearchVectorSearcher(index_resolver=lambda: index)
    vector = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    result = searcher.search_vectors(vector, limit=2)

    assert result == ["docs/a.md", "docs/b.md"]
    # The wrapper passed through the vector and limit verbatim.
    assert len(index.search_calls) == 1
    np.testing.assert_array_equal(index.search_calls[0][0], vector)
    assert index.search_calls[0][1] == 2


@pytest.mark.unit
def test_usearch_searcher_returns_empty_list_when_index_search_raises() -> None:
    """``index.search`` raising → wrapper returns ``[]``, does not propagate.

    The recall gate must be a soft alarm — production faults inside the
    vector index must not take down the embed pipeline.
    """

    class _ExplodingIndex:
        def search(self, vector: Any, *, k: int) -> list[dict[str, str]]:
            raise RuntimeError("usearch index corrupt")

    searcher = UsearchVectorSearcher(index_resolver=lambda: _ExplodingIndex())
    result = searcher.search_vectors(np.array([0.1, 0.2], dtype=np.float32), limit=3)
    assert result == []


@pytest.mark.unit
def test_usearch_searcher_returns_empty_list_when_index_resolver_raises() -> None:
    """``index_resolver`` itself raising (e.g. import-time crash) → ``[]``.

    The wrapper's outer try/except must cover the resolver lookup, not just
    the search call. Without this, a deferred-import error would crash the
    recall gate instead of degrading.
    """

    def _exploding_resolver() -> Any:
        raise ImportError("usearch wheel missing")

    searcher = UsearchVectorSearcher(index_resolver=_exploding_resolver)
    result = searcher.search_vectors(np.array([0.1, 0.2], dtype=np.float32), limit=3)
    assert result == []


# ---------------------------------------------------------------------------
# Lazy default constructions — RecallChecker._search builds a default
# UsearchVectorSearcher once when no vector_searcher was injected; the
# default credentials resolver returns None on exception.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_checker_lazily_constructs_default_vector_searcher_when_none_injected() -> None:
    """``RecallChecker(vector_searcher=None)`` builds a default
    ``UsearchVectorSearcher`` on first ``_search`` call.

    Observable contract: with no vector_searcher injected and a working
    embed provider, ``check()`` runs to completion (no crash) and reports
    the queries as non-skipped (the default usearch wrapper degrades to
    ``[]`` when the index is missing, which is a miss, not a skip).
    """
    checker = RecallChecker(embed_provider=FakeEmbedProvider(vector=[1.0, 0.0, 0.0]))

    result = checker.check(
        recall_queries=[
            ("R1", "q1", "no-match"),
            ("R2", "q2", "also-no-match"),
        ]
    )

    # Both queries reached the search step (non-skipped), and both missed —
    # the default ``UsearchVectorSearcher`` was constructed and used.
    assert result["total"] == 2
    assert result["passed"] == 0
    for entry in result["detail"]:
        assert entry["skipped"] is False
        assert entry["hit"] is False
        # The default searcher returned an empty list (no usearch index in unit env).
        assert entry["returned"] == []


@pytest.mark.unit
def test_check_skips_when_creds_resolver_itself_raises() -> None:
    """A ``creds_resolver`` that raises (e.g. Key Vault unreachable) is
    caught by the recall gate and surfaces as a skipped query.

    This is the alarm-system contract: the gate must NEVER take down the
    embed pipeline. A failing creds lookup degrades to "no recall this
    cycle", not an unhandled exception.
    """

    def _raising_resolver() -> object:
        raise RuntimeError("Azure Key Vault unreachable")

    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=_raising_resolver,
    )

    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["detail"][0]["skipped"] is True
    assert result["total"] == 0


@pytest.mark.unit
def test_production_default_creds_resolver_swallows_exceptions() -> None:
    """``_default_creds_resolver`` (production default) returns ``None``
    rather than propagating when ``get_credentials`` raises.

    Driven via the public RecallChecker surface: instantiate a checker with
    NO ``creds_resolver`` override; the default is in play. We then verify
    the contract by ensuring the gate skips every query when there are no
    real credentials configured (the test environment doesn't carry valid
    Azure secrets, so ``get_credentials`` raises OSError or returns
    something unusable).
    """
    # No embed_provider → forces lazy resolution via _default_creds_resolver.
    # No creds_resolver / provider_factory overrides → exercises production defaults.
    checker = RecallChecker(vector_searcher=FakeVectorSearcher())
    result = checker.check(recall_queries=[("R1", "any", "x")])

    # The default resolver caught the secrets-store failure and returned None,
    # so the gate skipped the query rather than crashing.
    assert result["detail"][0]["skipped"] is True
    assert result["total"] == 0


@pytest.mark.unit
def test_production_default_provider_factory_runs_when_creds_resolve(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production default ``provider_factory`` is invoked when creds
    resolve to a valid Credentials but no ``provider_factory`` override
    is supplied.

    The unit test environment doesn't carry Azure / OpenAI credentials, so
    the default factory's ``get_embed_provider()`` call raises OSError
    (missing env vars). The gate catches the exception, logs the
    "provider_factory raised" warning, and skips the query.

    This pins the wiring: a working creds_resolver + the production
    factory default DOES reach the SDK construction code.
    """
    import logging

    caplog.set_level(logging.WARNING, logger="kairix.core.embed.recall_check")

    # Inject valid-looking FakeCredentials so the resolver short-circuit
    # doesn't fire; no provider_factory override → production default runs.
    checker = RecallChecker(
        vector_searcher=FakeVectorSearcher(),
        creds_resolver=lambda: FakeCredentials(),
    )
    result = checker.check(recall_queries=[("R1", "any", "x")])

    # Query was skipped — the factory raised and the gate degraded gracefully.
    assert result["detail"][0]["skipped"] is True
    # The "provider_factory raised" warning was logged; this proves the
    # production default factory was reached and exercised, not the
    # missing-credentials short-circuit.
    messages = [rec.getMessage() for rec in caplog.records]
    assert any("provider_factory raised" in m for m in messages), (
        f"expected production default factory to be reached and raise; got {messages}"
    )
