"""
Tests for kairix.platform.llm — LLM backend abstraction (P1-2).

Uses ``LLMBackendDeps`` for dependency injection — no monkey-patching needed.
"""

from __future__ import annotations

import pytest

from kairix.platform.llm import AzureOpenAIBackend, get_default_backend
from kairix.platform.llm.backends import LLMBackendDeps
from kairix.platform.llm.protocol import LLMBackend
from tests.fakes import FakeLLMBackend

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
