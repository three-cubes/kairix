"""F68 failure-mode contract for the ``SocketModeTransport`` Protocol.

``SlackSocketModeHandler`` owns the Slack Socket Mode lifecycle on top
of this narrow transport (slack.md §5). Each transport method's failure
must land in an operator-visible state, never a crash or a silent loop:

* ``open`` failing transiently → backoff + retry, then ``POLL_ONLY``
  once the reconnect budget is spent; failing with
  ``CredentialExpiredError`` → ``DISCONNECTED`` + the cc_pair callback.
* ``iter_events`` dropping mid-stream → events already received are
  delivered, then the reconnect path runs.
* ``ack`` failing → the event is still delivered (Slack redelivers
  un-acked events; dropping it would lose data).
* ``close`` failing → swallowed; the handler still reaches its target
  state.

F43 parity: no production ``SocketModeTransport`` lives in kairix (the
real one is ``slack_sdk``'s client, wired by the operator's runtime), so
each body is run over two distinct real-world failure shapes through the
canonical ``FakeSocketModeTransport`` via a parametrized fixture.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.connectors.slack.socket_mode import SlackSocketModeHandler, SocketModeEvent, SocketModeState
from kairix.core.protocols import CredentialExpiredError
from tests.fakes import FakeClock, FakeSocketModeTransport

pytestmark = pytest.mark.contract

_BUDGET = 3


def _envelope(envelope_id: str) -> dict[str, Any]:
    return {"envelope_id": envelope_id, "payload": {"event": {"type": "message", "text": "rollout starts"}}}


@pytest.fixture(
    params=[
        ConnectionResetError("websocket reset by peer"),
        TimeoutError("websocket ping timed out"),
    ],
    ids=["connection-reset", "ping-timeout"],
)
def transport_error(request: pytest.FixtureRequest) -> BaseException:
    """Two real-world transient WebSocket failures each body runs over."""
    error: BaseException = request.param
    return error


@pytest.fixture(params=[0, 1], ids=["first-open", "after-one-session"])
def sessions_before_revoke(request: pytest.FixtureRequest) -> int:
    """How many clean sessions run before the install is revoked."""
    return int(request.param)


class _Harness:
    """One handler driving one fake transport, with recorders."""

    def __init__(self, transport: FakeSocketModeTransport) -> None:
        self.transport = transport
        self.delivered: list[SocketModeEvent] = []
        self.expired_calls = 0
        self.clock = FakeClock()

        def _expired() -> None:
            self.expired_calls += 1

        self.handler = SlackSocketModeHandler(
            transport_factory=lambda: transport,
            on_event=self.delivered.append,
            on_credential_expired=_expired,
            sleeper=self.clock.sleep,
            rand=lambda: 0.0,
            reconnect_fail_budget=_BUDGET,
        )


def test_open_raises_transient_error_exhausts_budget_into_poll_only(transport_error: BaseException) -> None:
    """``raises``: a WebSocket that never opens backs off between
    attempts, then trips to ``POLL_ONLY`` once the budget is spent — the
    poll surface keeps ingest alive instead of a silent retry loop.

    Sabotage proof (executed): change ``if attempt >=
    self.reconnect_fail_budget`` to ``>`` in ``connect`` → the test
    fails (one extra open attempt and backoff wait). Restored.
    """
    harness = _Harness(FakeSocketModeTransport(open_script=(transport_error,)))
    harness.handler.connect()
    assert harness.handler.state == SocketModeState.POLL_ONLY
    assert harness.handler.reconnect_attempts_total == _BUDGET
    assert harness.transport.open_calls == _BUDGET
    assert harness.clock.waits == [1.0, 2.0]
    assert harness.expired_calls == 0


def test_open_unauthorized_revoked_install_disconnects_and_flags_cc_pair(sessions_before_revoke: int) -> None:
    """``unauthorized``: a revoked install (``CredentialExpiredError``)
    is never retried — the handler fires the credential-expired callback
    once and parks in ``DISCONNECTED``.

    Sabotage proof (executed): make the ``except
    CredentialExpiredError`` clause in ``connect`` unreachable → the
    error is treated as transient and the handler ends in ``POLL_ONLY`` with the
    callback never fired. Restored.
    """
    script: tuple[BaseException | None, ...] = (
        *([None] * sessions_before_revoke),
        CredentialExpiredError("token_revoked"),
    )
    harness = _Harness(FakeSocketModeTransport(open_script=script))
    harness.handler.connect()
    assert harness.handler.state == SocketModeState.DISCONNECTED
    assert harness.expired_calls == 1
    assert harness.transport.open_calls == sessions_before_revoke + 1


def test_iter_events_raises_mid_stream_delivers_received_events_then_reconnects(
    transport_error: BaseException,
) -> None:
    """``raises``: a stream that drops after one event still delivers
    that event, counts the drop as a reconnect, and — with every reopen
    failing — ends in ``POLL_ONLY``.

    Sabotage proof (executed): drop the ``self.on_event(event)`` call
    in ``_drain`` → ``delivered`` is empty. Restored.
    """
    harness = _Harness(
        FakeSocketModeTransport(
            open_script=(None, transport_error),
            events=(_envelope("env-1"),),
            iter_raises=transport_error,
        )
    )
    harness.handler.connect()
    assert [event.envelope_id for event in harness.delivered] == ["env-1"]
    assert harness.handler.state == SocketModeState.POLL_ONLY
    assert harness.handler.reconnect_attempts_total == _BUDGET


def test_ack_raises_event_is_still_delivered(transport_error: BaseException) -> None:
    """``raises``: a failed ack is best-effort — the event is still
    handed to the connector (Slack redelivers un-acked envelopes, so
    dropping it here would lose the message).

    Sabotage proof (executed): narrow the ``except`` around
    ``self._transport.ack`` in ``_drain`` so the ack error escapes →
    it aborts the session and ``delivered`` is empty. Restored.
    """
    harness = _Harness(
        FakeSocketModeTransport(
            open_script=(None, transport_error),
            events=(_envelope("env-1"), _envelope("env-2")),
            ack_raises=transport_error,
        )
    )
    harness.handler.connect()
    assert harness.transport.acked == ["env-1", "env-2"]
    assert [event.envelope_id for event in harness.delivered] == ["env-1", "env-2"]


def test_close_raises_during_fail_over_still_reaches_poll_only(transport_error: BaseException) -> None:
    """``raises``: a ``close()`` that blows up while the handler fails
    over is swallowed — the handler still reports ``POLL_ONLY`` so the
    operator surface isn't stuck on ``RECONNECTING``.

    Sabotage proof (executed): narrow the ``except`` around
    ``self._transport.close()`` in ``fail_over_to_poll_only`` → the
    close error escapes ``connect`` and the state stays
    ``RECONNECTING``. Restored.
    """
    harness = _Harness(FakeSocketModeTransport(open_script=(transport_error,), close_raises=transport_error))
    harness.handler.connect()
    assert harness.transport.close_calls == 1
    assert harness.handler.state == SocketModeState.POLL_ONLY
