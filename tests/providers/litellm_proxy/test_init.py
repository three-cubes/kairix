"""Unit tests for :mod:`kairix.providers.litellm_proxy` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target — through
its ``credentials_resolver`` kwarg. Tests pass a stub resolver via the
public kwarg.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.litellm_proxy import LiteLLMProxyProvider, make_provider


@pytest.mark.unit
def test_make_provider_returns_litellm_proxy_provider_on_happy_path() -> None:
    """A resolved ``Credentials`` produces a ``LiteLLMProxyProvider``.

    To verify: comment out the ``return LiteLLMProxyProvider(...)``
    line — function returns ``None`` and the isinstance assert fails.
    """
    fake_creds = Credentials(
        api_key="virtual-key",  # pragma: allowlist secret
        endpoint="http://localhost:4000/v1",
        model="azure/foundry-deploy",
        dims=1536,
    )

    def _resolver(purpose: str) -> Credentials:
        assert purpose == "embed", f"litellm_proxy resolves embed purpose, got {purpose!r}"
        return fake_creds

    provider = make_provider(credentials_resolver=_resolver)
    assert isinstance(provider, LiteLLMProxyProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "litellm_proxy"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials() -> None:
    """A non-``Credentials`` resolver result surfaces a typed ``RuntimeError``."""

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError, match="did not resolve to a Credentials"):
        make_provider(credentials_resolver=_resolver)


@pytest.mark.unit
def test_make_provider_error_message_carries_actionable_markers() -> None:
    """The RuntimeError identifies the plugin and carries F21 markers."""

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError) as exc_info:
        make_provider(credentials_resolver=_resolver)

    msg = str(exc_info.value)
    assert "litellm_proxy" in msg
    assert "fix:" in msg
    assert "next:" in msg
