"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`PollConnector`.

One method (``list_changes_for_container``). Failure surface:

  * ``raises`` — surfaces a typed exception when the source backend
    (Graph delta endpoint, CRM API, …) raises mid-iteration; the
    orchestrator must NOT silently fall back to "no changes" because
    that would lose data.
  * ``returns_empty`` — empty iterator when no changes since the
    container's cursor; callers iterate without a None check.

F43 parity: each test runs ONE body over the REAL
:class:`LinearConnector` (a :class:`FakeLinearApiClient` injected
through its ``client_builder`` seam — no network) AND the canonical
:class:`FakePollConnector`.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.connectors.linear.connector import LinearConnector, LinearCredentials
from kairix.core.protocols import Container, PollConnector
from tests.fakes import FakeLinearApiClient, FakePollConnector, FakeSecretsLoader

pytestmark = pytest.mark.contract

_CREDS = LinearCredentials(api_key="lin_f68_fixture")  # pragma: allowlist secret — test fixture


def _issue_node() -> dict[str, Any]:
    return {
        "id": "uuid-issue-f68",
        "identifier": "ENG-68",
        "title": "F68 issue",
        "description": "Issue body for the failure-mode contract.",
        "url": "https://linear.app/your-team/issue/ENG-68",
        "createdAt": "2026-05-20T09:00:00.000Z",
        "updatedAt": "2026-05-22T10:00:00.000Z",
        "state": {"name": "Backlog"},
        "creator": {"displayName": "agent-alpha", "email": "agent-alpha@example.com"},
        "team": {"name": "Engineering"},
        "labels": {"nodes": []},
    }


def _container(cursor_token: str | None) -> Container:
    return Container(
        cc_pair_id=1,
        container_id="linear-workspace",
        access_state="ACCESSIBLE",
        cursor_token=cursor_token,
        last_synced_at=None,
    )


def _real(api: FakeLinearApiClient) -> LinearConnector:
    return LinearConnector(credentials=_CREDS, client_builder=lambda _c: api, secrets=FakeSecretsLoader())


@pytest.fixture(params=["real", "fake"])
def failing_poll(request: pytest.FixtureRequest) -> PollConnector:
    """A poll connector whose source backend fails mid-poll."""
    boom = RuntimeError("F68-poll-raises")
    if request.param == "fake":
        return FakePollConnector(raises=boom)
    return _real(FakeLinearApiClient(paginate_raises=boom))


@pytest.fixture(params=["real", "fake"])
def caught_up_poll(request: pytest.FixtureRequest) -> tuple[PollConnector, Container]:
    """A poll connector + a container whose cursor is already at HEAD.

    Real: one tick drains the seeded issue; the container then carries
    the connector's advanced cursor, so the next poll has nothing new.
    """
    if request.param == "fake":
        return FakePollConnector(), _container(None)
    conn = _real(FakeLinearApiClient(pages={"issues": [[_issue_node()]]}))
    drained = list(conn.list_changes_for_container(_container(None)))
    assert drained, "setup: the first tick must drain the seeded issue"
    return conn, _container(conn.next_cursor())


def test_list_changes_for_container_raises_propagates_typed_exception(failing_poll: PollConnector) -> None:
    """A delta-poll backend failure surfaces — orchestrator must NOT
    interpret a silent empty iterator as "no changes" when the source
    actually crashed (that would skip a sync window and lose data).

    Sabotage proof: in ``LinearConnector._drain_spec``
    (kairix/connectors/linear/connector.py) wrap the ``self._api.paginate``
    loop in ``try/except Exception: return``. Re-run: the real leg's
    pytest.raises sees nothing. Restored.
    """
    with pytest.raises(RuntimeError, match="F68-poll-raises"):
        # Realise the iterator — Protocol methods that yield can defer
        # raising until iteration; the test pins both shapes by calling
        # then iterating.
        list(failing_poll.list_changes_for_container(container=_container(None)))


def test_list_changes_for_container_returns_empty_when_no_changes_since_cursor(
    caught_up_poll: tuple[PollConnector, Container],
) -> None:
    """Empty iterator when the cursor is already at HEAD — callers
    iterate without a None check.

    Sabotage proof: in ``LinearConnector.list_changes_for_container``
    pass ``None`` instead of ``container.cursor_token`` to
    ``list_changes``. Re-run: the real leg re-emits the already-drained
    issue and the ``== []`` assertion fails. Restored.
    """
    conn, container = caught_up_poll
    out = list(conn.list_changes_for_container(container=container))
    assert out == [], f"empty events must yield []; got {out!r}"
