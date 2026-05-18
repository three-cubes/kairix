"""Unit tests for :mod:`kairix.providers.openai` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target — through
its ``credentials_resolver`` kwarg. Tests pass a stub resolver via the
public kwarg.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.openai import OpenAIProvider, make_provider


@pytest.mark.unit
def test_make_provider_returns_openai_provider_on_happy_path() -> None:
    """A resolved ``Credentials`` produces an ``OpenAIProvider``.

    To verify: comment out the ``return OpenAIProvider(...)`` line —
    the function returns ``None`` and the isinstance assert fails.
    """
    fake_creds = Credentials(
        api_key="openai-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        model="text-embedding-3-large",
        dims=1536,
    )

    def _resolver(purpose: str) -> Credentials:
        assert purpose == "embed", f"openai resolves embed purpose, got {purpose!r}"
        return fake_creds

    provider = make_provider(credentials_resolver=_resolver)
    assert isinstance(provider, OpenAIProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "openai"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials() -> None:
    """A non-``Credentials`` return value surfaces a typed ``RuntimeError``.

    To verify: weaken the isinstance guard to ``True`` — the
    ``RuntimeError`` no longer fires and pytest.raises misses its
    match.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError, match="did not resolve to a Credentials"):
        make_provider(credentials_resolver=_resolver)


@pytest.mark.unit
def test_make_provider_error_message_names_openai_and_offers_recovery() -> None:
    """The ``RuntimeError`` identifies the plugin and carries F21 markers.

    To verify: strip ``openai`` / ``fix:`` / ``next:`` substrings from
    the message — the assertions below fail.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError) as exc_info:
        make_provider(credentials_resolver=_resolver)

    msg = str(exc_info.value)
    assert "openai" in msg.lower()
    assert "fix:" in msg
    assert "next:" in msg
