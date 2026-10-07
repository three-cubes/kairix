"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`HierarchyConnector`.

Single method: ``load_hierarchy(cc_pair_id) -> Iterator[HierarchyNode]``.

Failure shapes:

  * **returns_empty** — a cc_pair with no folders / channels / spaces
    (e.g. a freshly-onboarded source before the first sync) yields ONLY
    the root node: no child containers. Every shipped connector emits its
    root first (F58), so "empty" means "root and nothing under it" — never
    a bare empty iterator.
  * **raises** — backend failure surfaces; the receiver tracks the
    failure and falls back to source_uri-prefix derivation.

F43 parity: both bodies run over the real
:class:`kairix.connectors.slack.SlackConnector` (flag ON, Web API client
injected through the public ``web_client_factory=`` seam) AND the
canonical :class:`tests.fakes.FakeHierarchyConnector`.

Finding (fake-vs-real drift): the previous ``returns_empty`` test seeded
``FakeHierarchyConnector(nodes=[])`` and asserted a bare ``[]`` — a shape
no real HierarchyConnector produces (all eleven shipped connectors yield
the root node unconditionally). The parity body pins the shared shape
instead: root only, carrying the requested ``cc_pair_id``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

from kairix.connectors.slack import SlackChannel, SlackConnector, SlackMessage, SlackWebClient
from kairix.connectors.slack.connector import SlackCredentials
from kairix.core.protocols import HierarchyConnector, HierarchyNode
from tests.fakes import FakeFeatureFlagResolver, FakeHierarchyConnector

pytestmark = pytest.mark.contract

_IDS = ["real", "fake"]


class _ScriptedWebClient(SlackWebClient):
    """Web client subclass that bypasses HTTP entirely.

    ``list_raises`` makes ``conversations_list`` raise (the Slack Web API
    failing mid-walk, after the root node has already been emitted).
    """

    def __init__(self, *, channels: list[SlackChannel], list_raises: BaseException | None = None) -> None:
        self._channels = channels
        self._list_raises = list_raises

    def conversations_list(self, *, types: Any = None) -> Iterator[SlackChannel]:
        del types
        if self._list_raises is not None:
            raise self._list_raises
        yield from self._channels

    def conversations_history(
        self, *, channel_id: str, oldest: str | None = None, inclusive: bool = False
    ) -> Iterator[SlackMessage]:
        del channel_id, oldest, inclusive
        yield from ()


def _real(*, channels: list[SlackChannel], list_raises: BaseException | None = None) -> HierarchyConnector:
    """Real SlackConnector with the connector flag ON and a scripted Web client."""
    resolver = FakeFeatureFlagResolver().with_flag("connector_slack", True)

    def _builder(_c: SlackCredentials) -> SlackWebClient:
        return _ScriptedWebClient(channels=channels, list_raises=list_raises)

    return SlackConnector(
        credentials=SlackCredentials(bot_token="xoxb-test-fake-token-value"),
        web_client_factory=_builder,
        flag_reader=resolver.get,
    )


def _root(cc_pair_id: int) -> HierarchyNode:
    """The root node a connector with no child containers emits."""
    return HierarchyNode(
        cc_pair_id=cc_pair_id,
        raw_node_id="fake-root",
        raw_parent_id=None,
        display_name="Fake Root",
        link=None,
        node_type="FOLDER",
        external_access_json=None,
        sensitivity_hint=None,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real(channels=[]),
        lambda: FakeHierarchyConnector(nodes=[_root(42)]),
    ],
    ids=_IDS,
)
def test_load_hierarchy_returns_empty_when_cc_pair_has_no_nodes(factory: Callable[[], HierarchyConnector]) -> None:
    """A cc_pair with no child containers yields only its root — callers
    iterate without a null check and never see an orphaned child.

    Sabotage proof: in ``SlackConnector.load_hierarchy`` emit an extra
    sentinel CHANNEL node after the root. Re-ran: the real leg fails the
    ``len(nodes) == 1`` assertion. Restored.
    """
    conn = factory()
    nodes = list(conn.load_hierarchy(cc_pair_id=42))
    assert len(nodes) == 1, f"no-container cc_pair must yield only the root; got {nodes!r}"
    assert nodes[0].raw_parent_id is None
    assert nodes[0].cc_pair_id == 42


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real(channels=[], list_raises=RuntimeError("F68-hierarchy-raises")),
        lambda: FakeHierarchyConnector(nodes=[_root(1)], raises=RuntimeError("F68-hierarchy-raises")),
    ],
    ids=_IDS,
)
def test_load_hierarchy_raises_propagates_typed_exception(factory: Callable[[], HierarchyConnector]) -> None:
    """Backend failure surfaces — the search-layer receiver decides
    whether to degrade to source_uri-prefix derivation; the Protocol
    contract is "raise, don't silently empty".

    Sabotage proof: in ``SlackConnector._enumerate_member_channels`` widen
    ``except CredentialExpiredError`` to ``except Exception``. Re-ran: the
    real leg swallows the error and ``pytest.raises`` sees nothing. Restored.
    """
    conn = factory()
    with pytest.raises(RuntimeError, match="F68-hierarchy-raises"):
        list(conn.load_hierarchy(cc_pair_id=1))
