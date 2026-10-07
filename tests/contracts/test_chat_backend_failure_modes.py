"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ChatBackend`.

``ChatBackend.complete`` wraps an LLM chat-completion call. Every body
runs over BOTH the production eval adapter
(:class:`kairix.quality.eval.chat_backend.ProviderEvalChatBackend`,
wrapping a :class:`tests.fakes.FakeProvider` plugin) and the canonical
:class:`tests.fakes.FakeChatBackend` (F43 behavioural parity).

Parity finding (PLA-472): the Protocol docstring says implementations
"raise on credential failure", but the production eval adapter is
never-raises — a provider-plugin failure is logged and surfaces as
``""`` (which ``LLMJudge`` maps to all-zero grades). The fake's default
``raise_on_call=`` shape therefore models a behaviour the shipped
adapter does not exhibit; the fake gained a ``swallow_errors=True``
mode faithful to the adapter, and this contract pins THAT shared
observable.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import ChatBackend
from kairix.quality.eval.chat_backend import ProviderEvalChatBackend
from tests.fakes import FakeChatBackend, FakeProvider

pytestmark = pytest.mark.contract

_CALL_KWARGS = {"api_key": "", "endpoint": "https://example.invalid", "deployment": "gpt-fake"}

# A factory takes ``(reply, error)``: the assistant text a healthy
# backend returns, or the credential error its provider raises.
ChatFactory = Callable[[str, Exception | None], ChatBackend]


def _real_backend(reply: str, error: Exception | None) -> ChatBackend:
    return ProviderEvalChatBackend(FakeProvider(chat_reply=reply, chat_raises=error))


def _fake_backend(reply: str, error: Exception | None) -> ChatBackend:
    return FakeChatBackend(responses=[reply], raise_on_call=error, swallow_errors=True)


_IMPLEMENTATIONS: list[tuple[str, ChatFactory]] = [
    ("real", _real_backend),
    ("fake", _fake_backend),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_complete_returns_empty_when_provider_credentials_fail(name: str, factory: ChatFactory) -> None:
    """A credential failure inside the provider surfaces as ``""`` —
    never fabricated content, never an unhandled crash out of the eval
    loop.

    Sabotage proof (executed): in ``ProviderEvalChatBackend.complete``
    change the ``except`` branch's ``return ""`` to ``return "fabricated"``.
    Re-run: the ``real`` case fails. Restored.
    """
    backend = factory("unused", ValueError("F68-chat-no-credentials"))
    assert backend.complete("hello", **_CALL_KWARGS) == "", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_complete_returns_empty_when_provider_replies_empty(name: str, factory: ChatFactory) -> None:
    """An empty assistant reply round-trips as ``""`` and a non-empty one
    verbatim — the backend neither invents content for the empty case nor
    rewrites a real reply.

    Sabotage proof: in ``ProviderEvalChatBackend.complete`` return
    ``self._provider.chat(...) or "default"``. Re-run: the ``real`` case
    fails on the empty reply. Restored.
    """
    assert factory("", None).complete("hello", **_CALL_KWARGS) == "", name
    assert factory('{"A": 2}', None).complete("hello", system="judge", **_CALL_KWARGS) == '{"A": 2}', name
