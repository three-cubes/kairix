"""Contract: ``AzureFoundryProvider.chat`` public kwarg surface is stable.

Pins kairix's public chat signature so the internal reasoning-model
translation (``max_tokens`` → ``max_completion_tokens`` on the wire for
gpt-5.x / o1-x / o3-x deployments) never leaks into the public kwarg
surface that callers — the rest of kairix, MCP tools, eval harnesses
— rely on.

If this contract fails, kairix callers either (a) suddenly have to know
which model is wired and pass the right kwarg name, or (b) silently
miss the parameter because they passed the kwarg the model now rejects.
Either way, the regression is invisible until a 400 lands in production.

The reasoning-model translation is INTENTIONALLY internal — it lives
in the provider's transport layer, not the public Protocol.
"""

from __future__ import annotations

import inspect

import pytest

from kairix.providers.azure_foundry import AzureFoundryProvider

pytestmark = pytest.mark.contract


def test_chat_keeps_max_tokens_as_public_kwarg() -> None:
    """``provider.chat(messages, *, max_tokens=N)`` — public kwarg name
    stays ``max_tokens`` regardless of which model is wired.

    Sabotage-proof: rename the public kwarg to ``max_completion_tokens``
    (intending to align with the OpenAI reasoning models) and every
    kairix caller silently breaks because they pass ``max_tokens=``.
    """
    sig = inspect.signature(AzureFoundryProvider.chat)
    assert "max_tokens" in sig.parameters, (
        f"AzureFoundryProvider.chat must keep ``max_tokens`` as a public kwarg; "
        f"got params: {sorted(sig.parameters.keys())}"
    )


def test_chat_does_not_expose_max_completion_tokens_as_public_kwarg() -> None:
    """The reasoning-model parameter name is internal-only.

    Allowing ``max_completion_tokens`` as a second public kwarg would
    force every caller to know which model is wired, defeating the
    purpose of having a provider abstraction at all.

    Sabotage-proof: add ``max_completion_tokens`` to the chat method's
    signature (intending to "support both") — this test fails and the
    callers get the same name to use regardless of underlying model.
    """
    sig = inspect.signature(AzureFoundryProvider.chat)
    assert "max_completion_tokens" not in sig.parameters, (
        f"AzureFoundryProvider.chat must not expose ``max_completion_tokens`` as a "
        f"public kwarg; the reasoning-model translation is internal. "
        f"Got params: {sorted(sig.parameters.keys())}"
    )


def test_max_tokens_is_keyword_only() -> None:
    """``max_tokens`` must be keyword-only — positional callers would
    create coupling that breaks when we add other internal-translation
    kwargs later.

    Sabotage-proof: remove the ``*,`` from the chat signature and this
    fails because the parameter becomes POSITIONAL_OR_KEYWORD.
    """
    sig = inspect.signature(AzureFoundryProvider.chat)
    param = sig.parameters["max_tokens"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY, f"max_tokens must be keyword-only; got kind={param.kind!r}"
