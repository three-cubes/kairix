"""Unit tests for :mod:`kairix.providers.anthropic` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target that
production callers reach through
``kairix.providers.get_provider("anthropic")``. The factory:

- resolves ``credentials_resolver("llm")`` (anthropic is chat-only),
- raises :class:`RuntimeError` with an actionable message when the
  resolver returns something other than a :class:`Credentials`,
- constructs an :class:`AnthropicProvider` against the resolved creds
  on the happy path.

Tests pass a stub resolver via the public ``credentials_resolver``
kwarg.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.anthropic import AnthropicProvider, make_provider


@pytest.mark.unit
def test_make_provider_returns_anthropic_provider_on_happy_path() -> None:
    """A resolved ``Credentials`` produces an ``AnthropicProvider``.

    To verify: comment out the ``return AnthropicProvider(...)`` line
    — the function returns ``None`` and the isinstance assert fails.
    """
    fake_creds = Credentials(
        api_key="anthropic-key",  # pragma: allowlist secret
        endpoint="https://api.anthropic.com",
        model="claude-3-5-sonnet-20241022",
        dims=0,
    )

    def _resolver(purpose: str) -> Credentials:
        assert purpose == "llm", f"anthropic resolves llm purpose, got {purpose!r}"
        return fake_creds

    provider = make_provider(credentials_resolver=_resolver)
    assert isinstance(provider, AnthropicProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "anthropic"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials() -> None:
    """A non-``Credentials`` resolver result surfaces a typed ``RuntimeError``.

    To verify: weaken the isinstance guard to ``True`` — the
    ``RuntimeError`` no longer fires.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError, match="did not resolve to a Credentials"):
        make_provider(credentials_resolver=_resolver)


@pytest.mark.unit
def test_make_provider_error_message_names_anthropic_and_offers_recovery() -> None:
    """The RuntimeError identifies the plugin and carries F21 markers.

    To verify: strip ``anthropic`` / ``fix:`` / ``next:`` substrings
    from the message — the assertions below fail.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError) as exc_info:
        make_provider(credentials_resolver=_resolver)

    msg = str(exc_info.value)
    assert "anthropic" in msg.lower()
    assert "fix:" in msg
    assert "next:" in msg
