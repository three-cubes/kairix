"""Unit tests for :mod:`kairix.providers.anthropic` entry-point factory.

Covers ``make_provider()`` — the entry-point discovery target that
production callers reach through
``kairix.providers.get_provider("anthropic")``. The factory:

- resolves ``get_credentials("llm")`` (anthropic is chat-only),
- raises :class:`RuntimeError` with an actionable message when the
  resolver returns something other than a :class:`Credentials`
  (defensive guard — :func:`get_credentials` is typed to always
  return a typed result, but operators occasionally short-circuit
  the secrets layer with stubs that return ``None``),
- constructs an :class:`AnthropicProvider` against the resolved creds
  on the happy path.

Test seam: direct attribute reassignment of
``kairix.credentials.get_credentials`` (stdlib-shape attribute swap,
not ``@patch`` — F1-clean; not a ``monkeypatch.setenv`` — F2-clean).
The same pattern is used in ``tests/core/test_factory.py`` for
``client_mod.get_client``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import kairix.credentials as credentials_module
from kairix.credentials import Credentials
from kairix.providers import Provider
from kairix.providers.anthropic import AnthropicProvider, make_provider


@pytest.fixture
def _swap_get_credentials() -> Iterator[Any]:  # yields a setter for the fake get_credentials function
    """Yield a setter that temporarily overrides ``credentials_module.get_credentials``.

    The yielded callable accepts a single replacement function; the
    fixture restores the original on teardown so other tests aren't
    affected by leaks.
    """
    saved = credentials_module.get_credentials

    def _set(replacement: Any) -> None:
        credentials_module.get_credentials = replacement

    try:
        yield _set
    finally:
        credentials_module.get_credentials = saved


@pytest.mark.unit
def test_make_provider_returns_anthropic_provider_on_happy_path(
    _swap_get_credentials: Any,
) -> None:
    """A resolved ``Credentials`` produces an ``AnthropicProvider``.

    Sabotage-proof: comment out the ``return AnthropicProvider(...)``
    line at the end of ``make_provider`` — the function would fall off
    the end and return ``None``, the ``isinstance(provider, Provider)``
    assertion fails (``None`` is not a Provider).
    """
    fake_creds = Credentials(
        api_key="anthropic-key",  # pragma: allowlist secret
        endpoint="https://api.anthropic.com",
        model="claude-3-5-sonnet-20241022",
        dims=0,
    )

    def _stub(purpose: str) -> Credentials:
        assert purpose == "llm", f"anthropic.make_provider must resolve llm purpose, got {purpose!r}"
        return fake_creds

    _swap_get_credentials(_stub)

    provider = make_provider()

    assert isinstance(provider, AnthropicProvider)
    assert isinstance(provider, Provider)
    assert provider.name == "anthropic"


@pytest.mark.unit
def test_make_provider_raises_runtime_error_when_resolver_returns_non_credentials(
    _swap_get_credentials: Any,
) -> None:
    """A non-``Credentials`` resolver result trips the defensive guard.

    Sabotage-proof: delete the ``if not isinstance(creds, Credentials)``
    block — ``make_provider`` would silently pass a ``None`` (or
    arbitrary object) into ``AnthropicProvider(credentials=...)``,
    breaking later attribute access (``credentials.api_key``) deep
    inside ``chat()`` with an opaque ``AttributeError`` instead of
    surfacing the actionable RuntimeError here. The
    ``pytest.raises(RuntimeError, match="fix: configure")`` clause
    fails on the wrong exception class.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError, match="fix: configure"):
        make_provider()


@pytest.mark.unit
def test_make_provider_error_message_names_anthropic_and_offers_recovery(
    _swap_get_credentials: Any,
) -> None:
    """The RuntimeError message identifies the plugin and points at recovery.

    Sabotage-proof: strip ``"anthropic:"`` or ``"fix:"`` / ``"next:"``
    affordance markers from the message — F21 requires actionable
    feedback; the assert on each substring fails.
    """
    _swap_get_credentials(lambda _purpose: None)

    with pytest.raises(RuntimeError) as exc_info:
        make_provider()

    msg = str(exc_info.value)
    assert "anthropic" in msg.lower()
    assert "fix:" in msg
    assert "next:" in msg
