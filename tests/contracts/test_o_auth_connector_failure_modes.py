"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`OAuthConnector`.

Two classmethods on the three-legged-OAuth flow surface:

  * ``oauth_authorization_url(state)`` — operator-visit URL builder.
  * ``oauth_code_to_token(code)`` — code→token-envelope builder.

The Protocol pins the SHAPE of the flow (classmethods, NOT instance
methods) because the flow happens BEFORE the connector instance exists.
Failure surface:

  * ``raises`` — a connector with no three-legged consent flow
    (client-credentials / app-only auth) surfaces a typed, actionable
    ``NotImplementedError`` from both classmethods instead of handing
    the operator a malformed URL or an empty token envelope.

F43 parity: each test runs ONE body over the REAL client-credentials
connectors that ship the OAuthConnector shim (SharePoint, M365 calendar,
M365 email headers) AND the canonical
:class:`FakeClientCredentialsOAuthConnector`.
"""

from __future__ import annotations

import pytest

from kairix.connectors.m365_calendar.connector import M365CalendarConnector
from kairix.connectors.m365_email_headers.connector import M365EmailHeadersConnector
from kairix.connectors.sharepoint.connector import SharePointConnector
from kairix.core.protocols import OAuthConnector
from tests.fakes import FakeClientCredentialsOAuthConnector

pytestmark = pytest.mark.contract

_CLIENT_CREDENTIALS_CONNECTORS = [
    pytest.param(SharePointConnector, id="real-sharepoint"),
    pytest.param(M365CalendarConnector, id="real-m365_calendar"),
    pytest.param(M365EmailHeadersConnector, id="real-m365_email_headers"),
    pytest.param(FakeClientCredentialsOAuthConnector, id="fake"),
]


@pytest.mark.parametrize("connector", _CLIENT_CREDENTIALS_CONNECTORS)
def test_oauth_authorization_url_raises_propagates_typed_exception(connector: type[OAuthConnector]) -> None:
    """A URL-builder failure surfaces — callers must NOT redirect the
    operator to a malformed URL. The error is actionable (F21 ``fix:``).

    Sabotage proof: change ``SharePointConnector.oauth_authorization_url``
    (kairix/connectors/sharepoint/connector.py) to ``return ""``. Re-run:
    the real-sharepoint leg's pytest.raises sees nothing. Restored.
    """
    with pytest.raises(NotImplementedError, match="client-credentials flow only") as exc_info:
        connector.oauth_authorization_url("state-value")
    assert "fix:" in str(exc_info.value)


@pytest.mark.parametrize("connector", _CLIENT_CREDENTIALS_CONNECTORS)
def test_oauth_code_to_token_raises_propagates_typed_exception(connector: type[OAuthConnector]) -> None:
    """A code-exchange failure surfaces — callers must NOT silently
    return an empty token dict because the next request would 401 with
    no diagnostic context.

    Sabotage proof: change ``M365CalendarConnector.oauth_code_to_token``
    (kairix/connectors/m365_calendar/connector.py) to ``return {}``.
    Re-run: the real-m365_calendar leg's pytest.raises sees nothing.
    Restored.
    """
    with pytest.raises(NotImplementedError, match="client-credentials flow only") as exc_info:
        connector.oauth_code_to_token("code-value")
    assert "fix:" in str(exc_info.value)
