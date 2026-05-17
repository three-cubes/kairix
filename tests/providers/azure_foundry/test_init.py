"""Unit tests for :mod:`kairix.providers.azure_foundry` entry-point factory.

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
from kairix.providers.azure_foundry import AzureFoundryProvider, make_provider


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
def test_make_provider_returns_azure_foundry_provider_on_happy_path(
    _swap_get_credentials: Any,
) -> None:
    """A resolved ``Credentials`` produces an ``AzureFoundryProvider``.

    Sabotage-proof: comment the ``return AzureFoundryProvider(...)``
    — function returns ``None`` and the isinstance assert fails.
    """
    fake_creds = Credentials(
        api_key="foundry-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com",
        model="text-embedding-3-large",
        dims=1536,
    )

    def _stub(purpose: str) -> Credentials:
        assert purpose == "embed", f"azure_foundry resolves embed purpose, got {purpose!r}"
        return fake_creds

    _swap_get_credentials(_stub)

    provider = make_provider()

    assert isinstance(provider, AzureFoundryProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "azure_foundry"


@pytest.mark.unit
def test_make_provider_raises_when_resolver_returns_non_credentials(
    _swap_get_credentials: Any,
) -> None:
    """Non-``Credentials`` resolver output trips the defensive guard.

    Sabotage-proof: removing the ``isinstance(creds, Credentials)``
    guard silently passes ``None`` deep into the provider and breaks
    later attribute access; the RuntimeError class assertion fails.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError, match="fix: configure"):
        make_provider()


@pytest.mark.unit
def test_make_provider_error_message_carries_affordance(
    _swap_get_credentials: Any,
) -> None:
    """Message identifies the plugin and includes recovery affordance.

    Sabotage-proof: dropping ``fix:`` / ``next:`` markers breaks the
    F21 actionable-feedback contract.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError) as exc_info:
        make_provider()

    msg = str(exc_info.value)
    assert "azure_foundry" in msg
    assert "fix:" in msg
    assert "next:" in msg
