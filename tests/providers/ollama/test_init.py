"""Unit tests for :mod:`kairix.providers.ollama` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target — through
its ``credentials_resolver`` kwarg. Tests pass a stub resolver via the
public kwarg.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.ollama import OllamaProvider, make_provider


@pytest.mark.unit
def test_make_provider_returns_ollama_provider_on_happy_path() -> None:
    """A resolved ``Credentials`` (ollama shape: empty api_key) produces an ``OllamaProvider``.

    To verify: comment out the ``return OllamaProvider(...)`` line —
    function returns ``None`` and the isinstance assert fails.
    """
    fake_creds = Credentials(
        api_key="",  # ollama is unauthenticated — empty key is canonical
        endpoint="http://localhost:11434",
        model="nomic-embed-text",
        dims=768,
    )

    def _resolver(purpose: str) -> Credentials:
        assert purpose == "embed", f"ollama resolves embed purpose, got {purpose!r}"
        return fake_creds

    provider = make_provider(credentials_resolver=_resolver)
    assert isinstance(provider, OllamaProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "ollama"


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
    assert "ollama" in msg
    assert "fix:" in msg
    assert "next:" in msg
