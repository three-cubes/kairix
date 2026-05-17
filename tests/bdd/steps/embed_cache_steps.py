"""Step definitions for embed_cache.feature.

Drives the real :class:`kairix.transport.embed_service.ProviderEmbeddingService`
(the public single-text embed surface) with a counting :class:`FakeProvider`
from ``tests/fakes.py`` at the plugin boundary. The cache is a real
:class:`kairix.transport.cache.EmbedCache` instance. F1-clean (no @patch
on kairix internals), F2-clean (no env monkeypatch — the fresh cache is
substituted via ``setattr`` on the package-public ``_EMBED_CACHE``
attribute), F5-clean (only public-surface imports).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.transport.cache.embed_cache import EmbedCache, reset_embed_cache
from kairix.transport.coalesce import reset_embed_coalescer
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeProvider

pytestmark = pytest.mark.bdd

# F17: lift repeated phrase fragments to constants.
_PHRASE_WRAPPER_WITH_CACHE = "an embed-text wrapper backed by an in-process embed cache"
_PHRASE_COUNTING_BACKEND = "a counting embed backend that records every call"


@pytest.fixture
def _ec_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Per-scenario fresh state.

    Resets the process-shared embed cache + coalescer between scenarios
    so cached entries from a previous scenario don't leak. The fresh
    cache is constructed directly (F2-clean: no env monkeypatch) and
    swapped into the module singleton via ``monkeypatch.setattr`` on a
    public attribute. The coalescer is reset to ``None`` so the
    ProviderEmbeddingService takes its direct-dispatch path (the
    coalescer's window-based batching adds non-determinism that this
    feature isn't pinning).
    """
    reset_embed_cache()
    reset_embed_coalescer()
    from kairix.transport.cache import embed_cache as embed_cache_mod

    fresh = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", fresh)

    state: dict[str, Any] = {"cache": fresh, "provider": None, "service": None}
    yield state
    reset_embed_cache()
    reset_embed_coalescer()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(_PHRASE_WRAPPER_WITH_CACHE)
def _given_wrapper(_ec_state: dict[str, Any]) -> None:
    """No-op — the cache is wired by the fixture above.

    The Given exists so the feature reads naturally; the wiring lives
    in :func:`_ec_state` so a single fixture owns lifecycle.
    """


@given(_PHRASE_COUNTING_BACKEND)
def _given_counting_backend(_ec_state: dict[str, Any]) -> None:
    """Construct a counting :class:`FakeProvider` and the adapter that
    wraps it, stashing both in scenario state.

    Embeds drive ``ProviderEmbeddingService.embed`` (the public single-text
    surface used by the search pipeline). The FakeProvider records every
    ``embed_batch`` call so the When/Then steps can pin call counts.
    """
    provider = FakeProvider(vector=[0.1, 0.2, 0.3])
    _ec_state["provider"] = provider
    _ec_state["service"] = ProviderEmbeddingService(provider)


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('agent "{agent}" embeds the text "{text}"'))
def _when_agent_embeds(_ec_state: dict[str, Any], agent: str, text: str) -> None:
    """Drive a real embed() call on the ProviderEmbeddingService.

    The embed cache is keyed on text only, so agent identity is
    irrelevant to the cache key — by design — and the test pins that
    by passing two different agents and asserting one backend call.
    The ``agent`` kwarg is accepted from the .feature so the scenario
    reads naturally, but it isn't forwarded anywhere (the embed cache
    intentionally doesn't shard on agent).
    """
    del agent  # F19: intentionally unused — see docstring
    _ec_state["service"].embed(text)


@when(parsers.parse('some caller embeds the text "{text}"'))
def _when_caller_embeds(_ec_state: dict[str, Any], text: str) -> None:
    """Same as the agent variant, without naming a caller."""
    _ec_state["service"].embed(text)


@when("some caller embeds an empty text")
def _when_caller_embeds_empty(_ec_state: dict[str, Any]) -> None:
    """The empty-string case can't be expressed cleanly via parsers.parse
    (which can't match an empty quoted token), so it gets its own step.
    """
    _ec_state["service"].embed("")


@when("some caller embeds a whitespace-only text")
def _when_caller_embeds_whitespace(_ec_state: dict[str, Any]) -> None:
    """Whitespace-only sibling of the empty-text step."""
    _ec_state["service"].embed("   ")


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the embed backend was called only once")
def _then_called_once(_ec_state: dict[str, Any]) -> None:
    """Sabotage: drop the cache.get() short-circuit in
    ProviderEmbeddingService.embed and every call hits the provider —
    embed_calls grows past 1 and this assertion fires.
    """
    calls = _ec_state["provider"].embed_calls
    assert len(calls) == 1, f"expected 1 embed call after cache hit; got {len(calls)}"


@then("the embed backend was not called")
def _then_not_called(_ec_state: dict[str, Any]) -> None:
    """Sabotage: remove the ``if not text or not text.strip()`` guard
    at the top of ProviderEmbeddingService.embed and empty strings hit
    the provider — the counter ticks up and the assertion fires.
    """
    calls = _ec_state["provider"].embed_calls
    assert len(calls) == 0, f"empty queries should never hit the embed backend; got {len(calls)}"


@then("the embed cache reports one hit and one miss")
def _then_one_hit_one_miss(_ec_state: dict[str, Any]) -> None:
    """Sabotage: skip the stats.hits / stats.misses increments and the
    observable counts diverge from the operator-facing reality.
    """
    stats = _ec_state["cache"].stats()
    assert stats.hits == 1, f"expected 1 hit; got {stats.hits}"
    assert stats.misses == 1, f"expected 1 miss; got {stats.misses}"


@then("the embed cache contains zero entries")
def _then_cache_empty(_ec_state: dict[str, Any]) -> None:
    """Sabotage: drop the empty-query guard in EmbedCache.put and the
    empty / whitespace texts land in the cache -> size grows above 0.
    """
    stats = _ec_state["cache"].stats()
    assert stats.size == 0, f"empty queries should NOT populate the cache; got size={stats.size}"
