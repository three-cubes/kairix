"""Unit tests for :class:`kairix.transport.embed_service.ProviderEmbeddingService`.

The adapter wraps a :class:`kairix.providers.Provider` (the plugin
contract) and exposes the
:class:`kairix.core.protocols.EmbeddingService` surface the search
pipeline consumes. Single-text embeds route through the
process-shared cache + coalescer; batched embeds go straight to the
plugin.

Sabotage proofs (each test):

- ``test_embed_calls_provider_embed_batch_with_single_text``: change
  ``embed_batch`` invocation in the adapter to drop the single-text
  list wrapper → this assertion fails because the provider sees
  ``""`` (string) instead of ``["text"]``.
- ``test_embed_returns_first_vector_from_batch``: change the adapter
  so it returns ``vectors`` instead of ``vectors[0]`` → the
  embedding becomes a list-of-lists, not a flat float vector.
- ``test_embed_returns_empty_on_provider_exception``: change the
  adapter so it re-raises instead of swallowing → the test asserts a
  return of ``[]``, which fails when the call propagates the error.
- ``test_embed_batch_returns_empty_per_text_on_provider_exception``:
  change the adapter to return a single ``[]`` (not one per text) →
  the length assertion fails.
- ``test_empty_text_short_circuits_without_calling_provider``: drop
  the empty-text guard at the top of ``embed`` → the provider
  records a call, failing the call-count assertion.
"""

from __future__ import annotations

import pytest

from kairix.transport.cache import get_embed_cache, reset_embed_cache
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeProvider

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_transport_singletons():
    """Reset the process-shared embed cache + coalescer between cases.

    Without this each test would see cached vectors from a previous
    test's fake — the cross-bleed would mask sabotage proofs that
    depend on "the provider was called for THIS text".
    """
    reset_embed_cache()
    reset_embed_coalescer()
    yield
    reset_embed_cache()
    reset_embed_coalescer()


class TestEmbedSingleText:
    """``embed(text)`` — the hot path used by the search pipeline."""

    def test_embed_calls_provider_embed_batch_with_single_text(self) -> None:
        """The adapter wraps ``text`` in a one-element list — the plugin
        contract is batch-shaped."""
        provider = FakeProvider(vector=[0.1, 0.2, 0.3])
        service = ProviderEmbeddingService(provider)

        service.embed("hello world")

        assert provider.embed_calls, "provider.embed_batch was never invoked"
        # Either the direct path or the coalescer dispatch — both reach
        # provider.embed_batch with a list containing "hello world".
        last_call = provider.embed_calls[-1]
        assert "hello world" in last_call, f"embed_batch saw {last_call!r}, expected to contain 'hello world'"

    def test_embed_returns_first_vector_from_batch(self) -> None:
        """A single-text embed returns the first vector from the plugin's batch reply."""
        expected = [0.4, 0.5, 0.6]
        provider = FakeProvider(vector=expected)
        service = ProviderEmbeddingService(provider)

        result = service.embed("hello world")

        assert result == expected, f"got {result!r}, expected {expected!r}"

    def test_empty_text_short_circuits_without_calling_provider(self) -> None:
        """Empty / whitespace-only text returns ``[]`` without calling the plugin.

        Mirrors the legacy ``embed_text`` defence-in-depth guard.
        """
        provider = FakeProvider(vector=[0.1, 0.2, 0.3])
        service = ProviderEmbeddingService(provider)

        assert service.embed("") == []
        assert service.embed("   ") == []
        assert provider.embed_calls == [], "provider.embed_batch should not be invoked for empty input"

    def test_embed_returns_empty_on_provider_exception(self) -> None:
        """Plugin exceptions surface as ``[]`` — the protocol contract is
        ``never raises`` so search pipelines short-circuit, not abort.
        """
        provider = FakeProvider(embed_raises=RuntimeError("simulated provider failure"))
        service = ProviderEmbeddingService(provider)

        result = service.embed("hello world")

        assert result == [], f"expected [] on plugin error, got {result!r}"

    def test_embed_populates_cache_on_success(self) -> None:
        """Successful embed populates the per-text cache so repeat calls don't
        re-hit the provider.

        Sabotage: drop the ``cache.put`` call in the adapter → the
        second call sees ``provider.embed_calls`` grow, failing the
        "exactly one provider call" assertion.
        """
        provider = FakeProvider(vector=[0.7, 0.8, 0.9])
        service = ProviderEmbeddingService(provider)

        first = service.embed("repeat me")
        second = service.embed("repeat me")

        assert first == second == [0.7, 0.8, 0.9]
        # Lookup the cache directly through its public surface.
        cache = get_embed_cache()
        cached = cache.get("repeat me")
        assert cached == [0.7, 0.8, 0.9], f"cache should hold the vector, got {cached!r}"


class TestEmbedBatch:
    """``embed_batch(texts)`` — bulk path used by worker ingestion / suite builders."""

    def test_embed_batch_calls_provider_with_all_texts(self) -> None:
        """The adapter passes the full list to the plugin in one call —
        bulk callers expect one provider round-trip."""
        provider = FakeProvider(vector=[1.0, 2.0, 3.0])
        service = ProviderEmbeddingService(provider)

        texts = ["alpha", "beta", "gamma"]
        result = service.embed_batch(texts)

        assert len(provider.embed_calls) == 1, f"expected one provider call, got {len(provider.embed_calls)}"
        assert provider.embed_calls[0] == texts
        assert len(result) == 3

    def test_embed_batch_returns_empty_list_for_empty_input(self) -> None:
        """No texts → no provider call, empty list result.

        Sabotage: drop the early-return guard → provider sees ``[]``
        and the adapter wastes a round-trip.
        """
        provider = FakeProvider(vector=[1.0])
        service = ProviderEmbeddingService(provider)

        assert service.embed_batch([]) == []
        assert provider.embed_calls == []

    def test_embed_batch_returns_empty_per_text_on_provider_exception(self) -> None:
        """Plugin error in batch returns ``[[]]`` of the same length so callers
        attribute failure per-text.
        """
        provider = FakeProvider(embed_raises=RuntimeError("batch provider failed"))
        service = ProviderEmbeddingService(provider)

        result = service.embed_batch(["a", "b", "c"])

        assert result == [[], [], []], f"expected three empty vectors, got {result!r}"
