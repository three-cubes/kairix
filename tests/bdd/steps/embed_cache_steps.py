"""Step definitions for embed_cache.feature.

Drives the real :func:`kairix.core.embed.embed_text` (the public
re-export) with a counting fake at the Azure-client boundary. The
cache is a real :class:`kairix.transport.cache.EmbedCache`
instance; the fake is passed via the public ``client=`` kwarg so the
test stays on the public surface (F5 - no private-name imports), no
@patch on internals (F1), no env monkeypatch (F2).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.embed import embed_text
from kairix.transport.cache.embed_cache import EmbedCache, reset_embed_cache

pytestmark = pytest.mark.bdd

# F17: lift repeated phrase fragments to constants.
_PHRASE_WRAPPER_WITH_CACHE = "an embed-text wrapper backed by an in-process embed cache"
_PHRASE_COUNTING_BACKEND = "a counting embed backend that records every call"
_TEST_DEPLOYMENT = "test-deployment"


class _EmbedItem:
    """Stub the openai SDK's response[i] shape - only ``.embedding`` is read."""

    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _EmbedResponse:
    """Stub the openai SDK's create() return shape - only ``.data[0]`` is read."""

    def __init__(self, embedding: list[float]) -> None:
        self.data = [_EmbedItem(embedding)]


class _CountingEmbeddings:
    """Tracks every embeddings.create() call so the test can pin call counts."""

    def __init__(self, owner: _CountingClient) -> None:
        self._owner = owner

    def create(self, *, model: str, input: list[str], dimensions: int) -> _EmbedResponse:
        self._owner.calls.append({"model": model, "input": list(input), "dimensions": dimensions})
        return _EmbedResponse([0.1, 0.2, 0.3])


class _CountingClient:
    """Fake openai-shaped client. ``.calls`` is the observable counter."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.embeddings = _CountingEmbeddings(self)


@pytest.fixture
def _ec_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Per-scenario fresh state.

    Resets the process-shared embed cache between scenarios so cached
    entries from a previous scenario don't leak. The fresh cache is
    constructed directly (F2-clean: no env monkeypatch) and swapped
    into the module singleton via ``monkeypatch.setattr`` on a public
    attribute.
    """
    reset_embed_cache()
    # Replace the process-shared singleton with a small one we own, so
    # the test's assertions on cache.stats() see ONLY this scenario.
    from kairix.transport.cache import embed_cache as embed_cache_mod

    fresh = EmbedCache(max_entries=10, max_age_s=60.0)
    monkeypatch.setattr(embed_cache_mod, "_EMBED_CACHE", fresh)

    state: dict[str, Any] = {"cache": fresh, "client": None}
    yield state
    reset_embed_cache()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(_PHRASE_WRAPPER_WITH_CACHE)
def _given_wrapper(_ec_state: dict[str, Any]) -> None:
    """No-op - the cache is wired by the fixture above.

    The Given exists so the feature reads naturally; the wiring lives
    in :func:`_ec_state` so a single fixture owns lifecycle.
    """


@given(_PHRASE_COUNTING_BACKEND)
def _given_counting_backend(_ec_state: dict[str, Any]) -> None:
    """Construct a counting client and stash it in scenario state.

    The client is passed to ``embed_text(client=...)`` by each When step
    - no module attribute mutation needed (F5-clean).
    """
    _ec_state["client"] = _CountingClient()


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('agent "{agent}" embeds the text "{text}"'))
def _when_agent_embeds(_ec_state: dict[str, Any], agent: str, text: str) -> None:
    """Drive a real embed_text() call.

    The embed cache is keyed on text only, so agent identity is
    irrelevant to the cache key - by design - and the test pins that
    by passing two different agents and asserting one backend call.
    The ``agent`` kwarg is accepted from the .feature so the scenario
    reads naturally, but it isn't forwarded anywhere (the embed cache
    intentionally doesn't shard on agent).
    """
    del agent  # F19: intentionally unused - see docstring
    embed_text(text, client=_ec_state["client"], deployment=_TEST_DEPLOYMENT)


@when(parsers.parse('some caller embeds the text "{text}"'))
def _when_caller_embeds(_ec_state: dict[str, Any], text: str) -> None:
    """Same as the agent variant, without naming a caller."""
    embed_text(text, client=_ec_state["client"], deployment=_TEST_DEPLOYMENT)


@when("some caller embeds an empty text")
def _when_caller_embeds_empty(_ec_state: dict[str, Any]) -> None:
    """The empty-string case can't be expressed cleanly via parsers.parse
    (which can't match an empty quoted token), so it gets its own step.
    """
    embed_text("", client=_ec_state["client"], deployment=_TEST_DEPLOYMENT)


@when("some caller embeds a whitespace-only text")
def _when_caller_embeds_whitespace(_ec_state: dict[str, Any]) -> None:
    """Whitespace-only sibling of the empty-text step."""
    embed_text("   ", client=_ec_state["client"], deployment=_TEST_DEPLOYMENT)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the embed backend was called only once")
def _then_called_once(_ec_state: dict[str, Any]) -> None:
    """Sabotage: drop the cache.get() short-circuit in embed_text and
    every call hits the backend - calls grows past 1 and this
    assertion fires.
    """
    calls = _ec_state["client"].calls
    assert len(calls) == 1, f"expected 1 embed call after cache hit; got {len(calls)}"


@then("the embed backend was not called")
def _then_not_called(_ec_state: dict[str, Any]) -> None:
    """Sabotage: remove the ``if not text or not text.strip()`` guard
    at the top of embed_text() and empty strings hit the client - the
    counter ticks up and the assertion fires.
    """
    calls = _ec_state["client"].calls
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
