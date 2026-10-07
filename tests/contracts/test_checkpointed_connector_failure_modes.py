"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`CheckpointedConnector`.

Single Protocol method ``load_from_checkpoint`` — yields
:class:`ChangeEvent` items resumed from an opaque checkpoint blob.
Two failure shapes worth pinning:

  * ``returns_empty`` — the connector has no events to yield from
    this checkpoint (e.g. caught up). Callers must distinguish empty
    from "checkpoint invalid" (which would raise).
  * ``raises`` — when the underlying source rejects the checkpoint
    (HTTP 410 gone, expired deltaLink), the exception must propagate
    so the orchestrator can drop to a full re-sync rather than
    silently swallow the broken state.

Every body runs over BOTH a shipped connector — the real
:class:`kairix.connectors.gmail.GmailConnector`, driven through its
public ``client=`` seam with :class:`tests.fakes.FakeGmailClient` — and
the canonical :class:`tests.fakes.FakeCheckpointedConnector` (F43
behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from kairix.connectors.gmail import GmailClient, GmailConnector
from kairix.core.protocols import CheckpointedConnector, Container
from tests.fakes import FakeCheckpointedConnector, FakeGmailClient

pytestmark = pytest.mark.contract

# A factory takes the error the source raises on a rejected checkpoint
# (``None`` = healthy source with nothing pending).
ConnectorFactory = Callable[[BaseException | None], CheckpointedConnector]


def _real_connector(error: BaseException | None) -> CheckpointedConnector:
    client = FakeGmailClient(messages=[], history_raises=error)
    return GmailConnector(user_email="agent-alpha@example.com", client=cast(GmailClient, client))


def _fake_connector(error: BaseException | None) -> CheckpointedConnector:
    return FakeCheckpointedConnector(events=[], raises=error)


_IMPLEMENTATIONS: list[tuple[str, ConnectorFactory]] = [
    ("real", _real_connector),
    ("fake", _fake_connector),
]


def _container() -> Container:
    return Container(
        cc_pair_id=1,
        container_id="agent-alpha@example.com",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_load_from_checkpoint_returns_empty_when_no_events_pending(name: str, factory: ConnectorFactory) -> None:
    """A connector with no events to yield from this checkpoint MUST
    return an empty iterator — callers tolerate empty as "caught up".
    Holds for the cold-start (``None``) checkpoint and a warm one.

    Sabotage proof: in ``GmailConnector.list_changes`` append a synthetic
    ``ChangeEvent`` before ``return iter(events)``. Re-run: the ``real``
    case fails because the iterator yields one event instead of zero.
    Restored.
    """
    conn = factory(None)
    for checkpoint in (None, "history-1000"):
        events = list(conn.load_from_checkpoint(_container(), checkpoint))
        assert events == [], f"{name}: caught-up connector must yield empty iterator; got {events!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_load_from_checkpoint_raises_on_expired_checkpoint(name: str, factory: ConnectorFactory) -> None:
    """When the source rejects the checkpoint (expired delta token,
    HTTP 410 Gone), the connector MUST raise — silent fallback to
    empty would hide the need for a full re-sync.

    Sabotage proof (executed): in ``GmailConnector.list_changes`` wrap the
    ``iter_history_message_ids`` loop in ``try/except Exception: pass``.
    Re-run: the ``real`` case fails because no exception fires. Restored.
    """
    conn = factory(RuntimeError("F68-checkpoint-expired"))
    with pytest.raises(RuntimeError, match="F68-checkpoint-expired"):
        list(conn.load_from_checkpoint(_container(), checkpoint="stale-delta-token"))
