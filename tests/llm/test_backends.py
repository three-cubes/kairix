"""
Tests for kairix.platform.llm — LLM backend abstraction (P1-2).

Uses ``LLMBackendDeps`` for dependency injection — no monkey-patching needed.
"""

from __future__ import annotations

import pytest

from kairix.core.protocols import ChatBackend
from kairix.platform.llm import AzureOpenAIBackend, get_default_backend
from kairix.platform.llm.backends import LLMBackendDeps
from kairix.platform.llm.protocol import LLMBackend
from kairix.quality.eval.chat_backend import ProviderEvalChatBackend
from tests.fakes import FakeLLMBackend, FakeProvider

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_azure_backend_conforms_to_protocol() -> None:
    backend = AzureOpenAIBackend()
    assert isinstance(backend, LLMBackend)


@pytest.mark.unit
def test_get_default_backend_returns_azure() -> None:
    backend = get_default_backend()
    assert isinstance(backend, AzureOpenAIBackend)


# ---------------------------------------------------------------------------
# AzureOpenAIBackend — delegates to injected callables
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_azure_backend_chat_delegates_to_injected_fn() -> None:
    calls = []

    def fake_chat(messages, max_tokens=800):
        calls.append((messages, max_tokens))
        return "Hi there"

    backend = AzureOpenAIBackend(deps=LLMBackendDeps(chat=fake_chat))
    messages = [{"role": "user", "content": "Hello"}]
    result = backend.chat(messages, max_tokens=100)

    assert len(calls) == 1
    assert calls[0] == (messages, 100)
    assert result == "Hi there"


@pytest.mark.unit
def test_azure_backend_chat_default_max_tokens() -> None:
    calls = []

    def fake_chat(messages, max_tokens=800):
        calls.append(max_tokens)
        return "ok"

    backend = AzureOpenAIBackend(deps=LLMBackendDeps(chat=fake_chat))
    backend.chat([{"role": "user", "content": "test"}])

    assert calls[0] == 800


@pytest.mark.unit
def test_azure_backend_embed_delegates_to_injected_fn() -> None:
    expected = [0.1, 0.2, 0.3]
    backend = AzureOpenAIBackend(deps=LLMBackendDeps(embed=lambda text: expected))
    result = backend.embed("some text")
    assert result == expected


@pytest.mark.unit
def test_azure_backend_chat_returns_empty_string_on_failure() -> None:
    backend = AzureOpenAIBackend(deps=LLMBackendDeps(chat=lambda msgs, max_tokens=800: ""))
    result = backend.chat([{"role": "user", "content": "test"}])
    assert result == ""


@pytest.mark.unit
def test_azure_backend_embed_returns_empty_list_on_failure() -> None:
    backend = AzureOpenAIBackend(deps=LLMBackendDeps(embed=lambda text: []))
    result = backend.embed("text")
    assert result == []


# ---------------------------------------------------------------------------
# Protocol usage pattern — callers receive LLMBackend, not concrete class
# ---------------------------------------------------------------------------


def _do_summarise(text: str, llm: LLMBackend) -> str:
    """Example of how production code should accept LLMBackend."""
    return llm.chat([{"role": "user", "content": f"Summarise: {text}"}])


@pytest.mark.unit
def test_caller_accepts_protocol_type() -> None:
    backend = AzureOpenAIBackend(deps=LLMBackendDeps(chat=lambda msgs, max_tokens=800: "Summary."))
    result = _do_summarise("long document", backend)
    assert result == "Summary."


@pytest.mark.unit
def test_canonical_fake_satisfies_protocol() -> None:
    """The canonical FakeLLMBackend in tests/fakes.py is structurally
    accepted as an LLMBackend — proves the Protocol contract is open to any
    matching shape, not tied to AzureOpenAIBackend.
    """
    fake = FakeLLMBackend(chat_response="mock response")
    assert isinstance(fake, LLMBackend)


@pytest.mark.unit
def test_caller_works_with_canonical_fake() -> None:
    fake = FakeLLMBackend(chat_response="mock response")
    result = _do_summarise("text", fake)
    assert result == "mock response"


# ---------------------------------------------------------------------------
# ProviderEvalChatBackend — adapts Provider plugin to eval ChatBackend protocol
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_provider_eval_chat_backend_satisfies_chat_backend_protocol() -> None:
    """The adapter structurally satisfies ``kairix.core.protocols.ChatBackend``.

    To verify: rename ``ProviderEvalChatBackend.complete`` to ``complete2`` —
    isinstance() against the Protocol returns False because ``complete`` is
    required.
    """
    backend = ProviderEvalChatBackend(FakeProvider(chat_reply="ok"))
    assert isinstance(backend, ChatBackend)


@pytest.mark.unit
def test_provider_eval_chat_backend_returns_provider_chat_reply() -> None:
    """The reply string comes from the wrapped provider's chat method.

    To verify: replace ``self._provider.chat(messages)`` with ``return ""``
    in ``ProviderEvalChatBackend.complete`` — the assertion on
    ``== "the-reply"`` fails because the test receives the empty default
    instead of the provider's configured response.
    """
    provider = FakeProvider(chat_reply="the-reply")
    backend = ProviderEvalChatBackend(provider)
    out = backend.complete("hello", api_key="k", endpoint="e", deployment="d")
    assert out == "the-reply"
    assert len(provider.chat_calls) == 1
    assert provider.chat_calls[0]["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.unit
def test_provider_eval_chat_backend_prepends_system_message_when_supplied() -> None:
    """When ``system`` is supplied it lands as the leading system message.

    To verify: drop the ``if system: messages.append(...)`` block in
    ``complete`` — the captured ``messages`` list omits the system entry
    and the length assertion fails.
    """
    provider = FakeProvider(chat_reply="reply")
    backend = ProviderEvalChatBackend(provider)
    backend.complete(
        "user prompt",
        api_key="k",
        endpoint="e",
        deployment="d",
        system="you are concise",
    )
    captured = provider.chat_calls[0]["messages"]
    assert captured == [
        {"role": "system", "content": "you are concise"},
        {"role": "user", "content": "user prompt"},
    ]


@pytest.mark.unit
def test_provider_eval_chat_backend_omits_system_message_when_not_supplied() -> None:
    """When ``system`` is ``None`` only the user message reaches the provider.

    To verify: change ``if system:`` to ``if True:`` — the captured
    messages list grows to length 2 with a ``None``-content system row.
    """
    provider = FakeProvider(chat_reply="reply")
    backend = ProviderEvalChatBackend(provider)
    backend.complete("user prompt", api_key="k", endpoint="e", deployment="d")
    captured = provider.chat_calls[0]["messages"]
    assert captured == [{"role": "user", "content": "user prompt"}]


# ---------------------------------------------------------------------------
# Production-default wiring — drive through the public ``AzureOpenAIBackend``
# constructor. When ``provider:`` is unset in ``kairix.config.yaml`` the
# default factory's lazy resolution raises ``ValueError``. The "configured
# provider routes through the plugin layer" behaviour is verified by
# ``tests/integration/test_provider_selection_e2e.py`` which loads a real
# yaml and exercises the full path; that's integration territory, not unit.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_backend_raises_value_error_on_chat_when_provider_not_configured() -> None:
    """Default-wired ``AzureOpenAIBackend()`` raises ValueError on ``chat()``.

    To verify: weaken the ``if name is None: raise`` guard in
    ``_resolve_provider`` — the typed ValueError no longer fires and the
    ``pytest.raises`` clause fails on a different exception.
    """
    backend = AzureOpenAIBackend()
    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        backend.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_default_backend_raises_value_error_on_embed_when_provider_not_configured() -> None:
    """Default-wired ``AzureOpenAIBackend()`` raises ValueError on ``embed()``."""
    backend = AzureOpenAIBackend()
    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        backend.embed("some text")


@pytest.mark.unit
def test_default_backend_chat_returns_provider_reply_when_deps_injected() -> None:
    """When a chat callable is injected via ``LLMBackendDeps`` the default
    factory is bypassed — proves the seam separation."""
    backend = AzureOpenAIBackend(
        deps=LLMBackendDeps(chat=lambda msgs, max_tokens=800: "x"),
    )
    assert backend.chat([{"role": "user", "content": "hi"}]) == "x"


# ---------------------------------------------------------------------------
# Production resolver — drive ``_default_chat`` / ``_default_embed`` through
# their public ``provider_resolver`` kwarg seam. This covers the
# post-ValueError lines in the resolver path (the ``return get_provider(name)``
# branch and the downstream ``ProviderChatBackend.chat`` / ``ProviderEmbeddingService.embed``
# delegations).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_chat_routes_through_resolver_provider() -> None:
    """``_default_chat`` calls the injected resolver and forwards to provider.

    To verify: drop the ``backend.chat(messages, max_tokens=...)`` line —
    the function returns ``""`` (its default empty string) and the equality
    assertion fails.
    """
    from kairix.platform.llm.backends import default_chat_callable

    fake = FakeProvider(name="wired", chat_reply="from resolver", vector=[1.0])
    out = default_chat_callable(
        [{"role": "user", "content": "hi"}],
        provider_resolver=lambda: fake,
    )
    assert out == "from resolver"
    assert len(fake.chat_calls) == 1


@pytest.mark.unit
def test_default_embed_routes_through_resolver_provider() -> None:
    """``_default_embed`` calls the injected resolver and forwards to provider.

    To verify: drop the ``svc.embed(text)`` line — the function returns
    ``[]`` and the equality assertion fails.
    """
    from kairix.platform.llm.backends import default_embed_callable
    from kairix.transport.cache import reset_embed_cache
    from kairix.transport.coalesce import reset_embed_coalescer

    reset_embed_cache()
    reset_embed_coalescer()
    try:
        fake = FakeProvider(name="wired", vector=[1.0, 2.0, 3.0])
        out = default_embed_callable("hello", provider_resolver=lambda: fake)
        assert out == [1.0, 2.0, 3.0]
        assert len(fake.embed_calls) >= 1
    finally:
        reset_embed_cache()
        reset_embed_coalescer()


@pytest.mark.unit
def test_resolve_provider_returns_provider_from_injected_fns() -> None:
    """``_resolve_provider`` uses the injected provider_name_fn + get_provider_fn.

    To verify: drop the ``return get_provider_fn(name)`` line —
    ``_resolve_provider`` falls off the end returning ``None`` and the
    isinstance assertion fails.
    """
    from kairix.platform.llm.backends import resolve_provider

    fake = FakeProvider(name="alpha")
    provider = resolve_provider(
        provider_name_fn=lambda: "alpha",
        get_provider_fn=lambda _name: fake,
    )
    assert provider is fake


@pytest.mark.unit
def test_resolve_provider_raises_when_provider_name_returns_none() -> None:
    """``_resolve_provider`` raises typed ValueError when name resolver returns None.

    To verify: replace the ``raise ValueError`` with a silent ``pass`` —
    the function then calls ``get_provider_fn(None)`` and the
    pytest.raises clause misses its match.
    """
    from kairix.platform.llm.backends import resolve_provider

    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        resolve_provider(
            provider_name_fn=lambda: None,
            get_provider_fn=lambda _name: None,
        )
