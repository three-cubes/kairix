"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EventConnector`.

Four Protocol methods: ``subscribe`` / ``renew_subscription`` /
``unsubscribe`` / ``handle_event``. Every body runs over BOTH a shipped
event connector — the real :class:`kairix.connectors.slack.SlackConnector`
in an Events-API deployment (no Socket Mode handler wired, so there is
no push surface to subscribe to) — and the canonical
:class:`tests.fakes.FakeEventConnector` configured the same way
(``supported=False``) (F43 behavioural parity).

Parity note (PLA-472): the previous ``renew_subscription`` probe asserted
a RAISE, proved only against an inline stub. No shipped EventConnector
raises on renew — Slack Socket Mode and GitHub App subscriptions carry
no TTL, so renew is a healthy no-op that returns the id. The contract
now pins that real observable under the ``unavailable`` class (no TTL /
no subscription surface to renew against).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.connectors.slack import SlackConnector, SlackCredentials
from kairix.core.protocols import EventConnector
from tests.fakes import FakeEventConnector

pytestmark = pytest.mark.contract


def _real_connector() -> EventConnector:
    # Events-API deployment: no ``socket_mode_handler_factory`` wired.
    return SlackConnector(credentials=SlackCredentials(bot_token="xoxb-test-fake-token-value"))


def _fake_connector() -> EventConnector:
    return FakeEventConnector(events=[], supported=False)


_IMPLEMENTATIONS: list[tuple[str, Callable[[], EventConnector]]] = [
    ("real", _real_connector),
    ("fake", _fake_connector),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_subscribe_returns_empty_when_source_unsupported(name: str, factory: Callable[[], EventConnector]) -> None:
    """Subscribe returns ``None`` to signal "no push surface for this
    deployment" — the framework falls back to polling.

    Sabotage proof (executed): in ``SlackConnector.subscribe`` change the
    no-factory ``return None`` to ``return ""``. Re-run: the ``real``
    case's ``is None`` assertion fails. Restored.
    """
    assert factory().subscribe("https://example.invalid/webhook") is None, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_renew_subscription_unavailable_returns_same_id_when_no_ttl(
    name: str, factory: Callable[[], EventConnector]
) -> None:
    """A subscription with no TTL to renew against reports healthy by
    returning the SAME id — the framework keeps its subscription rather
    than re-subscribing from scratch.

    Sabotage proof: in ``SlackConnector.renew_subscription`` return
    ``""``. Re-run: the ``real`` case fails. Restored.
    """
    assert factory().renew_subscription("sub-1") == "sub-1", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_unsubscribe_returns_empty_when_subscription_id_unknown(
    name: str, factory: Callable[[], EventConnector]
) -> None:
    """Unsubscribe is idempotent — an unknown id is a no-op returning
    ``None``, not a raise — on a first call and on a repeat.

    Sabotage proof: in ``SlackConnector.unsubscribe`` raise ``KeyError``
    when no handler is open. Re-run: the ``real`` case fails. Restored.
    """
    conn = factory()
    assert conn.unsubscribe("never-subscribed") is None, name
    assert conn.unsubscribe("never-subscribed") is None, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_handle_event_returns_empty_when_payload_carries_no_changes(
    name: str, factory: Callable[[], EventConnector]
) -> None:
    """An inbound webhook that carries no relevant changes yields
    nothing — callers iterate without a null check.

    Sabotage proof (executed): in ``SlackConnector.handle_event`` return
    a one-element iterator when no translator matches. Re-run: the
    ``real`` case's ``== []`` assertion fails. Restored.
    """
    assert list(factory().handle_event({"type": "noise", "noise": True})) == [], name
