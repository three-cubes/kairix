"""Branch coverage for :mod:`kairix.transport.embed_service`.

Covers the residual lines below the F7 90% floor in
``kairix/transport/embed_service.py``:

- ``ProviderEmbeddingService.embed`` — the existing-coalescer branch
  (lines 112-115): when a coalescer singleton is already installed,
  the adapter uses it directly instead of building a new one;
- ``ProviderEmbeddingService.embed`` — the no-coalescer fall-through
  branch (lines 127-136): when ``get_embed_coalescer`` returns None,
  the adapter dispatches directly through ``provider.embed_batch``
  and swallows transport errors per the "never raises" contract;
- ``ProviderChatBackend.__init__`` + ``chat`` (lines 183, 194-198):
  the chat adapter not previously covered.

Test seams:

- ``kairix.transport.coalesce.embed_coalescer._EMBED_COALESCER``
  module attribute, set via ``setattr`` per the documented
  pre-installation pattern in
  :func:`kairix.transport.coalesce.get_embed_coalescer`'s docstring
  ("tests pre-install their own EmbedCoalescer via setattr").
- Attribute reassignment of ``kairix.transport.coalesce.get_embed_coalescer``
  to drive the None-coalescer fall-through; stdlib-shape attribute
  swap, F1-clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import kairix.transport.coalesce as coalesce_module
from kairix.transport.cache import reset_embed_cache
from kairix.transport.coalesce import embed_coalescer as embed_coalescer_mod
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderChatBackend, ProviderEmbeddingService
from tests.fakes import FakeProvider


@pytest.fixture(autouse=True)
def _isolate_transport_singletons() -> Iterator[None]:
    """Reset cache + coalescer between cases (mirrors the existing pattern)."""
    reset_embed_cache()
    reset_embed_coalescer()
    yield
    reset_embed_cache()
    reset_embed_coalescer()


# ---------------------------------------------------------------------------
# Pre-installed coalescer singleton — lines 112-115
# ---------------------------------------------------------------------------


class _PreInstalledCoalescer:
    """Stand-in for ``EmbedCoalescer`` exposing the ``embed(text)`` surface.

    The production module's ``_EMBED_COALESCER`` is duck-typed —
    ``embed_service.py`` reads ``.embed(text)`` off whatever singleton
    is installed. We expose exactly that method and record calls so
    the test asserts the existing-coalescer branch fired (not the
    lazy-build branch).

    ``shutdown()`` exists for compatibility with
    :func:`reset_embed_coalescer` — the fixture teardown calls it
    on the registered singleton.
    """

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.embed_calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._vector)

    def shutdown(self) -> None:
        """No-op — mirror the real EmbedCoalescer surface for teardown."""


@pytest.mark.unit
def test_embed_uses_existing_coalescer_when_singleton_already_installed() -> None:
    """A pre-installed coalescer singleton is reused instead of building anew.

    Lines 110-115: ``existing = embed_coalescer_mod._EMBED_COALESCER``;
    if non-None, ``result = existing.embed(text)`` returns through the
    cache.

    Sabotage-proof: removing the ``if existing is not None: return
    existing.embed(...)`` block makes the adapter fall through to
    ``get_embed_coalescer(embed_batch=...)`` which would BUILD a new
    coalescer, and the pre-installed stand-in's ``embed_calls`` list
    would stay empty. The assert on ``pre_installed.embed_calls ==
    ["hello"]`` fails.
    """
    pre_installed = _PreInstalledCoalescer(vector=[0.1, 0.2, 0.3])
    embed_coalescer_mod._EMBED_COALESCER = pre_installed  # type: ignore[assignment] — pre-installed stand-in duck-types the .embed(text) surface the consumer reads, not the full EmbedCoalescer type

    provider = FakeProvider(vector=[9.9, 9.9, 9.9])  # different to detect bypass
    service = ProviderEmbeddingService(provider)

    result = service.embed("hello")

    assert result == [0.1, 0.2, 0.3]
    assert pre_installed.embed_calls == ["hello"], (
        f"existing-coalescer branch should have routed through pre-installed singleton; "
        f"got embed_calls={pre_installed.embed_calls!r}"
    )
    # The provider itself was NOT called — the singleton intercepted it.
    assert provider.embed_calls == []


@pytest.mark.unit
def test_embed_caches_result_from_existing_coalescer_when_non_empty() -> None:
    """Cache.put fires after the existing-coalescer branch returns a vector.

    Sabotage-proof: removing the ``if result: cache.put(text, result)``
    line in the existing-coalescer branch means a repeat call hits the
    pre-installed singleton again instead of the cache; the
    ``embed_calls`` list grows past 1.
    """
    pre_installed = _PreInstalledCoalescer(vector=[0.4, 0.5, 0.6])
    embed_coalescer_mod._EMBED_COALESCER = pre_installed  # type: ignore[assignment] — pre-installed stand-in duck-types the .embed(text) surface the consumer reads, not the full EmbedCoalescer type

    provider = FakeProvider(vector=[9.9, 9.9, 9.9])
    service = ProviderEmbeddingService(provider)

    service.embed("repeat me")
    service.embed("repeat me")

    # Cache absorbed the second call.
    assert len(pre_installed.embed_calls) == 1, (
        f"second call should hit the cache; pre-installed singleton was hit {len(pre_installed.embed_calls)} times"
    )


# ---------------------------------------------------------------------------
# No coalescer available — fall-through to direct dispatch (lines 127-136)
# ---------------------------------------------------------------------------


@pytest.fixture
def _swap_get_embed_coalescer() -> Iterator[Any]:
    """Yield a setter that reassigns ``kairix.transport.coalesce.get_embed_coalescer``.

    Tests in this file drive the "no coalescer available" branch by
    forcing the function to return ``None`` — exactly the path the
    embed-service comment describes as "the window=0 / sequential
    fallback". F1-clean (no ``@patch``); F2-clean (no
    ``monkeypatch.setenv``).
    """
    saved = coalesce_module.get_embed_coalescer

    def _set(replacement: Any) -> None:
        coalesce_module.get_embed_coalescer = replacement

    try:
        yield _set
    finally:
        coalesce_module.get_embed_coalescer = saved


@pytest.mark.unit
def test_embed_dispatches_directly_when_no_coalescer_available(
    _swap_get_embed_coalescer: Any,
) -> None:
    """``get_embed_coalescer`` returning None routes through the direct path.

    Lines 124-136: when no coalescer is installed AND
    ``get_embed_coalescer(...)`` returns None (the disabled /
    sequential path), the adapter calls ``provider.embed_batch([text])``
    directly and writes the vector into the cache.

    Sabotage-proof: removing the ``return embedding`` line at the end
    of this branch makes the function fall off the end and return
    ``None``; the equality assertion fails.
    """
    _swap_get_embed_coalescer(lambda **_kwargs: None)

    provider = FakeProvider(vector=[0.7, 0.8, 0.9])
    service = ProviderEmbeddingService(provider)

    result = service.embed("direct path")

    assert result == [0.7, 0.8, 0.9]
    # Direct dispatch — provider.embed_batch saw the single-text list.
    assert provider.embed_calls == [["direct path"]]


@pytest.mark.unit
def test_embed_returns_empty_on_provider_exception_in_direct_dispatch_path(
    _swap_get_embed_coalescer: Any,
) -> None:
    """Provider exception in the direct path is swallowed; return ``[]``.

    Drives the ``try / except Exception: return []`` block at
    lines 127-131.

    Sabotage-proof: removing the ``except Exception`` block propagates
    the RuntimeError; the test sees an uncaught exception, not ``[]``.
    """
    _swap_get_embed_coalescer(lambda **_kwargs: None)

    provider = FakeProvider(embed_raises=RuntimeError("direct path failed"))
    service = ProviderEmbeddingService(provider)

    result = service.embed("boom")

    assert result == []


@pytest.mark.unit
def test_embed_returns_empty_when_direct_dispatch_returns_empty_vectors(
    _swap_get_embed_coalescer: Any,
) -> None:
    """Empty plugin reply in the direct path → ``[]`` and no cache write.

    Drives line 133 (``if not vectors or not vectors[0]: return []``).

    Sabotage-proof: removing the empty-check guard would let
    ``embedding = list(vectors[0])`` IndexError-out; the test sees an
    exception rather than the clean ``[]`` sentinel.
    """
    _swap_get_embed_coalescer(lambda **_kwargs: None)

    # FakeProvider can be configured to return empty vectors. Looking
    # at fakes.py: setting ``vector=[]`` makes embed_batch return
    # ``[[]]`` per text — the inner-empty triggers the guard.
    provider = FakeProvider(vector=[])
    service = ProviderEmbeddingService(provider)

    result = service.embed("empty reply")

    assert result == []


# ---------------------------------------------------------------------------
# ProviderChatBackend — lines 182-198
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_chat_backend_delegates_chat_to_provider() -> None:
    """The chat adapter forwards ``chat(messages, max_tokens=N)`` to the plugin.

    Sabotage-proof: replacing the ``return self._provider.chat(...)``
    in ``ProviderChatBackend.chat`` with ``return ""`` makes the test
    see an empty string instead of the configured reply; the equality
    assertion fails.
    """
    provider = FakeProvider(chat_reply="the answer")
    backend = ProviderChatBackend(provider)

    out = backend.chat([{"role": "user", "content": "what is the answer?"}], max_tokens=42)

    assert out == "the answer"
    assert len(provider.chat_calls) == 1
    assert provider.chat_calls[0]["max_tokens"] == 42


class _ChatRaisingProvider:
    """A minimal ``Provider``-shaped fake whose ``chat`` always raises.

    ``tests/fakes.FakeProvider`` doesn't expose a ``chat_raises``
    kwarg today; rather than expanding the canonical fake for one
    test-local branch, this in-test class wraps the same shape with
    a raising ``chat`` so the embed-service branch test stays
    self-contained.
    """

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
        from kairix.providers import ProviderHealth

        return ProviderHealth(ok=False, endpoint="fake", error="ChatRaisingProvider")


@pytest.mark.unit
def test_provider_chat_backend_returns_empty_on_provider_exception() -> None:
    """Provider exception in chat is swallowed; return ``""``.

    Drives the ``try / except Exception: return ""`` block at lines
    194-198.

    Sabotage-proof: removing the ``except Exception`` block lets the
    RuntimeError propagate; the test sees an uncaught exception
    rather than the empty-string sentinel.
    """
    provider = _ChatRaisingProvider(RuntimeError("provider chat failed"))
    backend = ProviderChatBackend(provider)

    out = backend.chat([{"role": "user", "content": "hi"}])

    assert out == ""
