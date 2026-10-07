"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SlimConnector`.

One method (``retrieve_all_slim_docs``). Failure surface:

  * ``raises`` — surfaces typed exception when the source's id-listing
    endpoint fails; the prune cycle must NOT silently stage a full
    delete sweep on a transient backend error.
  * ``returns_empty`` — empty iterator when the container is empty.

F43: every test runs ONE assertion body over the real
:class:`kairix.connectors.slack.SlackConnector` (id-listing via
``conversations.history``, driven by the
:class:`tests.fakes.FakeSlackWebApi` MockTransport stub) AND
:class:`tests.fakes.FakeSlimConnector`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import Container, SlimConnector
from tests.fakes import FakeSlackWebApi, FakeSlimConnector

pytestmark = pytest.mark.contract

SlimFactory = Callable[[BaseException | None], SlimConnector]


def _container() -> Container:
    return Container(
        cc_pair_id=1,
        container_id="C0001",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )


def _real(raises: BaseException | None) -> SlimConnector:
    """Real Slack slim listing; a backend failure is a non-ok history page."""
    if raises is not None:
        api = FakeSlackWebApi(responses={"conversations.history": {"ok": False, "error": "F68_slim_raises"}})
    else:
        api = FakeSlackWebApi(responses={"conversations.history": {"messages": []}})
    connector: SlimConnector = api.build_connector()
    return connector


def _fake(raises: BaseException | None) -> SlimConnector:
    return FakeSlimConnector(raises=raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_all_slim_docs_raises_propagates_typed_exception(factory: SlimFactory) -> None:
    """A slim-listing backend failure surfaces — orchestrator must NOT
    interpret a silent empty list as "container is empty" because that
    would tombstone every document (catastrophic prune sweep).

    Sabotage proof: in ``kairix.connectors.slack.web_client._raise_for_payload_error``
    replace the final ``raise RuntimeError(...)`` with ``return``.
    Re-run: the real leg's pytest.raises sees nothing. Restored.
    """
    conn = factory(RuntimeError("slack: conversations.history returned ok=false: 'F68_slim_raises'"))
    with pytest.raises(RuntimeError, match="F68_slim_raises"):
        list(conn.retrieve_all_slim_docs(_container()))


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_retrieve_all_slim_docs_returns_empty_when_container_empty(factory: SlimFactory) -> None:
    """Empty iterator when the container has no items — the orchestrator
    tombstones nothing.

    Sabotage proof: in ``SlackConnector.retrieve_all_slim_docs`` add
    ``yield f"{container.container_id}:phantom"`` before the history
    loop. Re-run: the real leg's ``== []`` assertion fails. Restored.
    """
    conn = factory(None)
    out = list(conn.retrieve_all_slim_docs(_container()))
    assert out == [], f"empty container must yield []; got {out!r}"
