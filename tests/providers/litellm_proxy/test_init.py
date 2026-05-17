"""Unit tests for :mod:`kairix.providers.litellm_proxy` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target.

Test seam: direct attribute reassignment of
``kairix.credentials.get_credentials`` (F1-clean / F2-clean).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import kairix.credentials as credentials_module
from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.litellm_proxy import LiteLLMProxyProvider, make_provider


@pytest.fixture
def _swap_get_credentials() -> Iterator[Any]:
    saved = credentials_module.get_credentials

    def _set(replacement: Any) -> None:
        credentials_module.get_credentials = replacement

    try:
        yield _set
    finally:
        credentials_module.get_credentials = saved


@pytest.mark.unit
def test_make_provider_returns_litellm_proxy_provider_on_happy_path(
    _swap_get_credentials: Any,
) -> None:
    """A resolved ``Credentials`` (LiteLLM proxy shape) produces a ``LiteLLMProxyProvider``.

    Sabotage-proof: comment the ``return LiteLLMProxyProvider(...)``
    — function returns ``None`` and the isinstance assertion fails.
    """
    fake_creds = Credentials(
        api_key="litellm-virtual-key",  # pragma: allowlist secret
        endpoint="http://localhost:4000/v1",
        model="azure/gpt-4o-mini",
        dims=1536,
    )

    def _stub(purpose: str) -> Credentials:
        assert purpose == "embed", f"litellm_proxy resolves embed purpose, got {purpose!r}"
        return fake_creds

    _swap_get_credentials(_stub)

    provider = make_provider()

    assert isinstance(provider, LiteLLMProxyProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "litellm_proxy"


@pytest.mark.unit
def test_make_provider_raises_when_resolver_returns_non_credentials(
    _swap_get_credentials: Any,
) -> None:
    """Non-``Credentials`` resolver output trips the defensive guard.

    Sabotage-proof: drop the isinstance() guard — None propagates into
    LiteLLMProxyProvider and an AttributeError surfaces later instead
    of the actionable RuntimeError; the type assertion fails.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError, match="fix: configure"):
        make_provider()


@pytest.mark.unit
def test_make_provider_error_message_carries_affordance(
    _swap_get_credentials: Any,
) -> None:
    """Message names litellm_proxy and includes recovery affordance.

    Sabotage-proof: stripping ``fix:`` / ``next:`` markers breaks
    F21 actionable-feedback compliance.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError) as exc_info:
        make_provider()

    msg = str(exc_info.value)
    assert "litellm_proxy" in msg
    assert "fix:" in msg
    assert "next:" in msg
