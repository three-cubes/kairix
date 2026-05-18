"""Unit tests for :mod:`kairix.providers.azure_foundry` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target — through
its ``credentials_resolver`` kwarg. Tests pass a stub resolver via the
public kwarg; no module-attribute reassignment.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.azure_foundry import AzureFoundryProvider, make_provider


@pytest.mark.unit
def test_make_provider_returns_azure_foundry_provider_on_happy_path() -> None:
    """A resolved ``Credentials`` produces an ``AzureFoundryProvider``.

    To verify: comment out the ``return AzureFoundryProvider(...)``
    line in ``make_provider`` — the isinstance assertion below fails
    because the function falls off the end returning ``None``.
    """
    fake_creds = Credentials(
        api_key="foundry-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com",
        model="text-embedding-3-large",
        dims=1536,
    )

    seen_purposes: list[str] = []

    def _resolver(purpose: str) -> Credentials:
        seen_purposes.append(purpose)
        assert purpose in ("embed", "llm"), f"azure_foundry resolves embed + llm purposes, got {purpose!r}"
        return fake_creds

    provider = make_provider(credentials_resolver=_resolver)
    assert "embed" in seen_purposes, "make_provider must resolve embed credentials"
    assert "llm" in seen_purposes, (
        "make_provider must resolve llm credentials so chat() can use the LLM endpoint+model "
        "(separate from embed for project-scoped Foundry deployments)"
    )
    assert isinstance(provider, AzureFoundryProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "azure_foundry"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials() -> None:
    """A non-``Credentials`` return value surfaces a typed ``RuntimeError``.

    To verify: weaken the ``isinstance(creds, Credentials)`` guard to
    ``True`` — the ``RuntimeError`` no longer fires and pytest.raises
    misses its match, failing the assertion.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError, match="did not resolve to a Credentials"):
        make_provider(credentials_resolver=_resolver)


@pytest.mark.unit
def test_make_provider_error_message_carries_actionable_markers() -> None:
    """The RuntimeError message includes ``fix:`` and ``next:`` markers per F21.

    To verify: drop the ``fix:`` / ``next:`` substrings from the
    ``RuntimeError`` message — the assertions below fail.
    """

    def _resolver(_purpose: str) -> Any:
        return None

    with pytest.raises(RuntimeError) as exc_info:
        make_provider(credentials_resolver=_resolver)

    msg = str(exc_info.value)
    assert "azure_foundry" in msg
    assert "fix:" in msg
    assert "next:" in msg
