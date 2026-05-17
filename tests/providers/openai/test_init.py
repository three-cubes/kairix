"""Unit tests for :mod:`kairix.providers.openai` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target.

Test seam: direct attribute reassignment of
``kairix.credentials.get_credentials`` (F1-clean: not ``@patch``;
F2-clean: not ``monkeypatch.setenv``). Same pattern as
``tests/core/test_factory.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import kairix.credentials as credentials_module
from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.openai import OpenAIProvider, make_provider


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
def test_make_provider_returns_openai_provider_on_happy_path(
    _swap_get_credentials: Any,
) -> None:
    """A resolved ``Credentials`` produces an ``OpenAIProvider``.

    Sabotage-proof: comment out the ``return OpenAIProvider(...)``
    return — the function falls off the end returning ``None`` and
    the ``isinstance`` assertion fails.
    """
    fake_creds = Credentials(
        api_key="openai-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        model="text-embedding-3-large",
        dims=1536,
    )

    def _stub(purpose: str) -> Credentials:
        assert purpose == "embed", f"openai.make_provider must resolve embed purpose, got {purpose!r}"
        return fake_creds

    _swap_get_credentials(_stub)

    provider = make_provider()

    assert isinstance(provider, OpenAIProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "openai"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials(
    _swap_get_credentials: Any,
) -> None:
    """Non-``Credentials`` resolver output trips the defensive guard.

    Sabotage-proof: delete the ``if not isinstance(creds, Credentials)``
    block — make_provider silently passes ``None`` into
    ``OpenAIProvider(credentials=...)`` and breaks later attribute
    access deep inside embed/chat with an opaque AttributeError.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError, match="fix: configure"):
        make_provider()


@pytest.mark.unit
def test_make_provider_error_message_names_openai_and_offers_recovery(
    _swap_get_credentials: Any,
) -> None:
    """The RuntimeError message identifies the plugin and offers an affordance.

    Sabotage-proof: strip ``"openai:"`` or ``"fix:"`` markers from the
    message — F21 requires actionable feedback and the substring
    assertions fail.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError) as exc_info:
        make_provider()

    msg = str(exc_info.value)
    assert "openai" in msg.lower()
    assert "fix:" in msg
    assert "next:" in msg
