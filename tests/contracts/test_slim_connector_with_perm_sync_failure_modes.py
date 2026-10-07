"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SlimConnectorWithPermSync`.

One method (``retrieve_all_slim_docs_with_perms``). Failure surface:

  * ``raises`` — surfaces typed exception when the per-doc ACL endpoint
    fails (SharePoint Graph 429, Drive 403, etc.); orchestrator must
    NOT silently fall back to "no perms" because that would either
    grant blanket access OR mass-deny.
  * ``returns_empty`` — empty iterator when the container has no items
    requiring perm-sync.
  * ``unauthorized`` — typed exception when credentials are expired
    (perm-sync needs a separate auth scope than slim-listing).

F43: every test runs ONE assertion body over the real
:class:`kairix.connectors.slack.SlackConnector` (ACL via
``conversations.members`` + ids via ``conversations.history``, driven by
the :class:`tests.fakes.FakeSlackWebApi` MockTransport stub) AND
:class:`tests.fakes.FakeSlimConnectorWithPermSync`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import Container, CredentialExpiredError, SlimConnectorWithPermSync
from tests.fakes import FakeSlackWebApi, FakeSlimConnectorWithPermSync

pytestmark = pytest.mark.contract

PermFactory = Callable[[dict[str, Any], BaseException | None], SlimConnectorWithPermSync]


def _container() -> Container:
    return Container(
        cc_pair_id=1,
        container_id="C0002",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )


def _real(slack_responses: dict[str, Any], _raises: BaseException | None) -> SlimConnectorWithPermSync:
    """Real Slack perm-sync; the failure is scripted on the Web API wire."""
    connector: SlimConnectorWithPermSync = FakeSlackWebApi(responses=slack_responses).build_connector()
    return connector


def _fake(_slack_responses: dict[str, Any], raises: BaseException | None) -> SlimConnectorWithPermSync:
    return FakeSlimConnectorWithPermSync(raises=raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_all_slim_docs_with_perms_raises_propagates_typed_exception(factory: PermFactory) -> None:
    """A perm-sync backend failure surfaces — orchestrator must NOT
    interpret a silent empty list as "no permissions" (that would
    either grant blanket access or mass-deny).

    Sabotage proof: in ``kairix.connectors.slack.web_client._raise_for_payload_error``
    replace the final ``raise RuntimeError(...)`` with ``return``.
    Re-run: the real leg's pytest.raises sees nothing. Restored.
    """
    conn = factory(
        {"conversations.members": {"ok": False, "error": "F68_perm_raises"}},
        RuntimeError("slack: conversations.members returned ok=false: 'F68_perm_raises'"),
    )
    with pytest.raises(RuntimeError, match="F68_perm_raises"):
        list(conn.retrieve_all_slim_docs_with_perms(_container()))


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_all_slim_docs_with_perms_returns_empty_when_container_empty(factory: PermFactory) -> None:
    """Empty iterator when the container has no items requiring
    perm-sync — callers iterate without a null check.

    Sabotage proof: in ``SlackConnector.retrieve_all_slim_docs_with_perms``
    add ``yield "phantom", acl_serialised`` before the history loop.
    Re-run: the real leg's ``== []`` assertion fails. Restored.
    """
    conn = factory({"conversations.members": {"members": ["U_ALPHA"]}, "conversations.history": {"messages": []}}, None)
    out = list(conn.retrieve_all_slim_docs_with_perms(_container()))
    assert out == [], f"empty container must yield []; got {out!r}"


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_all_slim_docs_with_perms_unauthorized_raises_typed_exception(factory: PermFactory) -> None:
    """A perm-sync auth failure (expired credentials, revoked scope)
    surfaces as a typed :class:`CredentialExpiredError` so the
    operator-facing layer can re-prompt for consent rather than silently
    mass-revoking ACLs.

    Sabotage proof: in ``kairix.connectors.slack.web_client.SlackWebClient._post``
    drop the ``if response.status_code in (401, 403)`` branch. Re-run:
    the real leg raises ``httpx.HTTPStatusError`` instead of the typed
    error. Restored.
    """
    conn = factory(
        {"conversations.members": 401},
        CredentialExpiredError("slack: conversations.members returned HTTP 401 — workspace install rejected."),
    )
    with pytest.raises(CredentialExpiredError, match="401"):
        list(conn.retrieve_all_slim_docs_with_perms(_container()))
