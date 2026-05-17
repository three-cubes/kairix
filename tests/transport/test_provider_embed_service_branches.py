"""Branch coverage for :mod:`kairix.transport.embed_service`.

Covers the residual lines below the F7 90% floor in
``kairix/transport/embed_service.py``:

- ``ProviderEmbeddingService.embed`` — the existing-coalescer branch:
  a pre-installed coalescer is reused instead of building a new one;
- ``ProviderEmbeddingService.embed`` — the no-coalescer fall-through:
  when the factory returns ``None`` the adapter dispatches directly
  through ``provider.embed_batch`` and swallows transport errors;
- ``ProviderChatBackend.__init__`` + ``chat``: the chat adapter not
  previously covered.

All branches drive through the public seams on
``ProviderEmbeddingService``:

- ``existing_coalescer_fn`` kwarg — injects the singleton-lookup
- ``coalescer_factory`` kwarg — injects the lazy-build factory

Tests pass stubs through these kwargs; no module-attribute reassignment.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from kairix.transport.cache import reset_embed_cache
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderChatBackend, ProviderEmbeddingService
from tests.fakes import FakeProvider


@pytest.fixture(autouse=True)
def _isolate_transport_singletons() -> Iterator[None]:
    """Reset cache + coalescer between cases."""
    reset_embed_cache()
    reset_embed_coalescer()
    yield
    reset_embed_cache()
    reset_embed_coalescer()


# ---------------------------------------------------------------------------
# Pre-installed coalescer — drives the ``existing_coalescer_fn`` seam
# ---------------------------------------------------------------------------


class _RecordingCoalescer:
    """Stand-in for ``EmbedCoalescer`` exposing the ``embed(text)`` surface.

    Production reads ``.embed(text)`` off whatever singleton is installed.
    This stand-in records calls so the test asserts the existing-coalescer
    branch fired rather than the lazy-build branch.
    """

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.embed_calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._vector)


@pytest.mark.unit
def test_embed_uses_existing_coalescer_when_one_is_present() -> None:
    """The existing-coalescer branch routes through the pre-installed instance.

    To verify: drop the ``if existing is not None: return existing.embed(...)``
    block in ``ProviderEmbeddingService.embed`` — the adapter falls through
    to the lazy-build factory and the stand-in's ``embed_calls`` list
    stays empty.
    """
    pre_installed = _RecordingCoalescer(vector=[0.1, 0.2, 0.3])
    provider = FakeProvider(vector=[9.9, 9.9, 9.9])  # distinct to detect bypass

    service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: pre_installed,
    )

    result = service.embed("hello")

    assert result == [0.1, 0.2, 0.3]
    assert pre_installed.embed_calls == ["hello"]
    assert provider.embed_calls == []


@pytest.mark.unit
def test_embed_caches_result_from_existing_coalescer_so_second_call_skips() -> None:
    """Cache absorbs the second call after the existing-coalescer branch fires.

    To verify: drop the ``if result: cache.put(text, result)`` line in
    the existing-coalescer branch — the second call re-enters the
    coalescer and ``embed_calls`` grows past 1.
    """
    pre_installed = _RecordingCoalescer(vector=[0.4, 0.5, 0.6])
    provider = FakeProvider(vector=[9.9, 9.9, 9.9])

    service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: pre_installed,
    )

    service.embed("repeat me")
    service.embed("repeat me")

    assert len(pre_installed.embed_calls) == 1


# ---------------------------------------------------------------------------
# No coalescer available — drives the ``coalescer_factory`` seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_embed_dispatches_directly_when_factory_returns_none() -> None:
    """The no-coalescer fall-through routes through ``provider.embed_batch``.

    To verify: drop the ``return embedding`` at the end of the direct
    dispatch branch — the function falls off the end and returns
    ``None``, failing the equality check.
    """
    provider = FakeProvider(vector=[0.7, 0.8, 0.9])
    service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_kwargs: None,
    )

    result = service.embed("direct path")

    assert result == [0.7, 0.8, 0.9]
    assert provider.embed_calls == [["direct path"]]


@pytest.mark.unit
def test_embed_returns_empty_when_provider_raises_on_direct_dispatch() -> None:
    """Provider exception on the direct path is swallowed; ``[]`` returned.

    To verify: drop the ``except Exception`` block — the RuntimeError
    propagates and the test sees an uncaught exception instead of ``[]``.
    """
    provider = FakeProvider(embed_raises=RuntimeError("direct path failed"))
    service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_kwargs: None,
    )

    result = service.embed("boom")

    assert result == []


@pytest.mark.unit
def test_embed_returns_empty_when_direct_dispatch_yields_empty_vectors() -> None:
    """Empty plugin reply on the direct path → ``[]`` and no cache write.

    To verify: drop the ``if not vectors or not vectors[0]: return []``
    guard — ``embedding = list(vectors[0])`` IndexError-s and the test
    sees an exception instead of the clean ``[]`` sentinel.
    """
    provider = FakeProvider(vector=[])
    service = ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_kwargs: None,
    )

    result = service.embed("empty reply")

    assert result == []


# ---------------------------------------------------------------------------
# ProviderChatBackend
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_chat_backend_delegates_to_provider() -> None:
    """The chat adapter forwards ``chat(messages, max_tokens=N)`` to the plugin.

    To verify: replace the ``return self._provider.chat(...)`` line with
    ``return ""`` — the equality assertion fails because the test sees
    the empty default instead of the configured reply.
    """
    provider = FakeProvider(chat_reply="the answer")
    backend = ProviderChatBackend(provider)

    out = backend.chat([{"role": "user", "content": "what is the answer?"}], max_tokens=42)

    assert out == "the answer"
    assert len(provider.chat_calls) == 1
    assert provider.chat_calls[0]["max_tokens"] == 42


class _ChatRaisingProvider:
    """A minimal ``Provider``-shaped fake whose ``chat`` always raises."""

    name = "raising"

    def __init__(self, err: BaseException) -> None:
        self._err = err

    def embed_batch(self, _texts: list[str]) -> list[list[float]]:
        return []

    def chat(self, _messages: list[dict[str, object]], *, max_tokens: int = 800) -> str:
        del max_tokens
        raise self._err

    def dimension(self) -> int:
        return 1536

    def healthcheck(self) -> object:
        from kairix.providers._base import ProviderHealth

        return ProviderHealth(
            ok=False,
            endpoint="fake",
            cold_ms=None,
            warm_ms=None,
            error="never called",
        )


@pytest.mark.unit
def test_provider_chat_backend_returns_empty_string_on_provider_exception() -> None:
    """Plugin exception in chat is swallowed; ``""`` returned.

    To verify: drop the ``except Exception`` in ``ProviderChatBackend.chat``
    — the exception propagates instead of being mapped to ``""``.
    """
    backend = ProviderChatBackend(_ChatRaisingProvider(RuntimeError("chat boom")))

    out = backend.chat([{"role": "user", "content": "anything"}])

    assert out == ""
