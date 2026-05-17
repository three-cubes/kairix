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
    """Sanity: the canonical FakeLLMBackend in tests/fakes.py is structurally
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

    Sabotage: rename ``ProviderEvalChatBackend.complete`` to ``complete2`` —
    isinstance() against the Protocol returns False (the protocol declares
    ``complete`` as required) and this assertion fails.
    """
    backend = ProviderEvalChatBackend(FakeProvider(chat_reply="ok"))
    assert isinstance(backend, ChatBackend)


@pytest.mark.unit
def test_provider_eval_chat_backend_returns_provider_chat_reply() -> None:
    """The reply string comes from the wrapped provider's chat method.

    Sabotage: replace ``self._provider.chat(messages)`` with ``return ""``
    in ``ProviderEvalChatBackend.complete`` — the assert on ``== "the-reply"``
    fails because the test would receive the empty default instead of the
    provider's configured response.
    """
    provider = FakeProvider(chat_reply="the-reply")
    backend = ProviderEvalChatBackend(provider)
    out = backend.complete("hello", api_key="k", endpoint="e", deployment="d")
    assert out == "the-reply"
    # The provider was called exactly once with a single user message.
    assert len(provider.chat_calls) == 1
    assert provider.chat_calls[0]["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.unit
def test_provider_eval_chat_backend_prepends_system_message_when_supplied() -> None:
    """When ``system`` is supplied it lands as the leading system message.

    Sabotage: drop the ``if system: messages.append(...)`` block in
    ``complete`` — the captured ``messages`` list won't contain the
    system entry and the length assert fails.
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

    Sabotage: change ``if system:`` to ``if True:`` — the captured messages
    list grows to length 2 with a ``None``-content system row and this
    length-1 assertion fails.
    """
    provider = FakeProvider(chat_reply="reply")
    backend = ProviderEvalChatBackend(provider)
    backend.complete("user prompt", api_key="k", endpoint="e", deployment="d")
    captured = provider.chat_calls[0]["messages"]
    assert captured == [{"role": "user", "content": "user prompt"}]


@pytest.mark.unit
def test_provider_eval_chat_backend_drops_credential_and_tuning_kwargs() -> None:
    """Credential / tuning kwargs are accepted for protocol conformance and ignored.

    Sabotage: remove the ``del api_key, endpoint, deployment, temperature,
    timeout_s`` line and forward those kwargs to ``provider.chat`` —
    ``FakeProvider.chat`` accepts only ``max_tokens``, so propagating
    additional kwargs would raise TypeError and the test would fail with
    an exception instead of the clean reply.
    """
    provider = FakeProvider(chat_reply="x")
    backend = ProviderEvalChatBackend(provider)
    # Supplying non-trivial values exercises the "accepted but not propagated" branch.
    out = backend.complete(
        "p",
        api_key="K",
        endpoint="E",
        deployment="D",
        temperature=0.7,
        timeout_s=10.0,
    )
    assert out == "x"


# ---------------------------------------------------------------------------
# Production-default wiring — drive through the public AzureOpenAIBackend
# constructor (which builds LLMBackendDeps with the default-factory chat /
# embed callables behind the scenes). The test environment has no
# ``provider:`` field in ``kairix.config.yaml`` so the lazy provider
# resolution raises ValueError on first chat / embed call. This is the
# end-to-end receipt that the default callables wire through the provider
# plugin layer instead of importing ``kairix._azure`` directly.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_backend_raises_value_error_on_chat_when_provider_not_configured() -> None:
    """Default-wired ``AzureOpenAIBackend()`` raises ValueError on ``chat()``.

    The constructor builds production defaults (``LLMBackendDeps()`` field
    factories return the provider-backed callables). ``chat()`` triggers
    the lazy ``kairix.config.yaml`` lookup; the test config doesn't seed
    a ``provider:`` field so a typed ValueError fires.

    Sabotage: rewire the chat default to import ``kairix._azure`` (the
    pre-migration shape) — the test would then see a credential-resolution
    error from the legacy path, NOT the ValueError from the new
    config-missing path, and the ``pytest.raises(ValueError, match=...)``
    clause fails on the wrong exception or message.
    """
    backend = AzureOpenAIBackend()
    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        backend.chat([{"role": "user", "content": "hi"}])


@pytest.mark.unit
def test_default_backend_raises_value_error_on_embed_when_provider_not_configured() -> None:
    """Default-wired ``AzureOpenAIBackend()`` raises ValueError on ``embed()``.

    Sabotage: rewire the embed default to import ``kairix._azure.embed_text``
    (the pre-migration shape) — the test would see a different exception
    (credential error or empty-list return) and the match clause fails.
    """
    backend = AzureOpenAIBackend()
    with pytest.raises(ValueError, match="missing the required 'provider:' field"):
        backend.embed("some text")


@pytest.mark.unit
def test_default_backend_chat_returns_provider_reply_when_deps_injected() -> None:
    """When a chat callable is injected via ``LLMBackendDeps`` the default
    factory is bypassed, proving the seam separation: production wiring is
    end-to-end provider-backed; tests inject fakes through ``deps`` and
    never touch the provider resolution path.

    Sabotage: have ``AzureOpenAIBackend.chat`` always call the production
    default instead of ``self._deps.chat`` — the injected fake would be
    ignored, the provider lookup would raise, and this clean ``"x"`` reply
    assertion would fail with an exception.
    """
    backend = AzureOpenAIBackend(
        deps=LLMBackendDeps(chat=lambda msgs, max_tokens=800: "x"),
    )
    assert backend.chat([{"role": "user", "content": "hi"}]) == "x"
