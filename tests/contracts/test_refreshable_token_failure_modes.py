"""F68 failure-injection contract tests for :class:`RefreshableToken`.

* ``refresh`` → ``unavailable`` (network down, refresh_token revoked)
* ``headers`` → ``unavailable`` shape — propagates the refresh failure
  when the cached token is expired
* ``is_expired`` → ``returns_empty`` shape — pin behaviour when no
  initial access token was ever set (treated as expired)

F43: every test runs ONE assertion body over the real
:class:`kairix.connect.refresh.GoogleRefreshableToken` (failure injected
through its ``refresh_fn`` seam) AND
:class:`tests.fakes.FakeRefreshableToken`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.connect.protocols import RefreshableToken, RefreshUnavailableError
from kairix.connect.refresh import GoogleRefreshableToken, GoogleRefreshState
from tests.fakes import FakeRefreshableToken

pytestmark = pytest.mark.contract

TokenFactory = Callable[[BaseException | None], RefreshableToken]


def _state() -> GoogleRefreshState:
    return GoogleRefreshState(
        client_id="x",
        client_secret="y",
        refresh_token="z",
        token_uri="https://x/",
    )


def _real(refresh_raises: BaseException | None) -> RefreshableToken:
    """Real token with no initial access token; refresh fails with ``refresh_raises``."""

    def refresh_fn(_state: GoogleRefreshState, _existing: str | None) -> tuple[str, float]:
        if refresh_raises is not None:
            raise refresh_raises
        return "fresh-access-token", 4_102_444_800.0

    return GoogleRefreshableToken(state=_state(), refresh_fn=refresh_fn)


def _fake(refresh_raises: BaseException | None) -> RefreshableToken:
    """Fake token with no access token ever set; refresh fails with ``refresh_raises``."""
    return FakeRefreshableToken(token="", refresh_raises=refresh_raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_refresh_unavailable_when_network_down(factory: TokenFactory) -> None:
    """A ``refresh`` call that fails surfaces :class:`RefreshUnavailableError`.

    Sabotage proof: in ``kairix.connect.refresh.GoogleRefreshableToken.refresh``
    change ``raise RefreshUnavailableError(...) from exc`` to ``raise exc``
    (re-raise the raw ConnectionError). Re-run: the real leg fails
    because pytest.raises sees ConnectionError. Restored.
    """
    token = factory(ConnectionError("network unreachable"))
    with pytest.raises(RefreshUnavailableError, match="refresh failed"):
        token.refresh()


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_headers_unavailable_when_expired_token_refresh_fails(factory: TokenFactory) -> None:
    """``headers`` triggers refresh on stale token; refresh failure propagates.

    Sabotage proof: in ``GoogleRefreshableToken.headers`` drop the
    ``if self.is_expired(): self.refresh()`` guard. Re-run: the real leg
    returns a bearer header instead of raising. Restored.
    """
    token = factory(RuntimeError("simulated upstream unavailable"))
    # No initial access token → is_expired is True → headers() triggers refresh → raises
    with pytest.raises(RefreshUnavailableError):
        token.headers()


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_is_expired_returns_empty_when_no_token_ever_set(factory: TokenFactory) -> None:
    """With no initial token, ``is_expired`` reports True so callers know to refresh.

    Sabotage proof: in ``GoogleRefreshableToken.is_expired`` change
    ``if not self._access_token: return True`` to ``return False``.
    Re-run: the real leg's ``is True`` assertion fails. Restored.
    """
    token = factory(None)
    assert token.is_expired() is True
