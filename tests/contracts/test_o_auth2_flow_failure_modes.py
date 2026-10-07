"""F68 failure-injection contract tests for :class:`OAuth2Flow`.

Per ADR-032 §"Contract tests" each Protocol method gets a
failure-injection test naming the F68 shape in the function name:

  * ``discover_client_credentials`` → ``raises``
  * ``authorize`` → ``returns_partial``

F43 parity: each test runs ONE body over the REAL
:class:`GoogleOAuth2Flow` (browser + token exchanger injected through
its constructor seams) AND the canonical :class:`FakeOAuth2Flow`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.connect.oauth2.google import GOOGLE_TOKEN_URI, GoogleOAuth2Flow
from kairix.connect.protocols import CapturedTokens, ClientCredentials, OAuth2Flow
from tests.fakes import FakeBrowserLauncher, FakeCallbackListener, FakeOAuth2Flow

pytestmark = pytest.mark.contract

_PARTIAL_TOKENS = CapturedTokens(
    refresh_token="",  # partial — no long-lived credential
    access_token="short-lived-only",
    token_uri=GOOGLE_TOKEN_URI,
)


def _partial_exchanger(_c: ClientCredentials, _code: str, _ru: str) -> CapturedTokens:
    return _PARTIAL_TOKENS


@pytest.fixture(params=["real", "fake"])
def flow_missing_secret(request: pytest.FixtureRequest, tmp_path: Path) -> OAuth2Flow:
    """A flow pointed at an operator-supplied client-secret path that doesn't exist."""
    absent = tmp_path / "absent.json"
    if request.param == "fake":
        return FakeOAuth2Flow(service_area="gmail", client_secret_path=absent)
    return GoogleOAuth2Flow(service_area="gmail", client_secret_path=absent)


@pytest.fixture(params=["real", "fake"])
def flow_partial_grant(request: pytest.FixtureRequest, tmp_path: Path) -> OAuth2Flow:
    """A flow whose token exchange grants only a short-lived access token."""
    if request.param == "fake":
        return FakeOAuth2Flow(service_area="gmail", browser=FakeBrowserLauncher(), tokens=_PARTIAL_TOKENS)
    cs = tmp_path / "cs.json"
    cs.write_text('{"installed":{"client_id":"x","client_secret":"y"}}')
    return GoogleOAuth2Flow(
        service_area="gmail",
        client_secret_path=cs,
        browser=FakeBrowserLauncher(),
        token_exchanger=_partial_exchanger,
    )


def test_discover_client_credentials_raises_file_not_found_when_path_missing(
    flow_missing_secret: OAuth2Flow,
) -> None:
    """``discover_client_credentials`` raises when the operator-supplied path doesn't exist.

    Sabotage proof: in ``_parse_client_secret_file``
    (kairix/connect/oauth2/google.py) drop the ``if not path.exists():
    raise FileNotFoundError`` guard. Re-run: the real leg raises a
    different error (no ``client_secret.json not found`` match). Restored.
    """
    with pytest.raises(FileNotFoundError, match=r"client_secret\.json not found"):
        flow_missing_secret.discover_client_credentials()


def test_authorize_returns_partial_when_no_refresh_token_granted(flow_partial_grant: OAuth2Flow) -> None:
    """``authorize`` surfaces a partial :class:`CapturedTokens` (empty refresh_token).

    The contract is round-trip: the OAuth2Flow returns whatever the
    exchanger gave it. Caller-side validation (e.g. the connector
    refusing to store an empty refresh_token) lives at the caller, not
    inside the flow.

    Sabotage proof: in ``AuthorizationCodeFlow.authorize``
    (kairix/connect/oauth2/base.py) replace the returned tokens with a
    ``CapturedTokens`` carrying a synthesised refresh_token. Re-run:
    the real leg's ``refresh_token == ""`` assertion fails. Restored.
    """
    tokens = flow_partial_grant.authorize(listener=FakeCallbackListener())
    assert tokens.refresh_token == ""
    assert tokens.access_token == "short-lived-only"
