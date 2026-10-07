"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Resolver`.

One method (``reindex``). Failure surface:

  * ``raises`` — surfaces typed exception when the per-item refetch
    fails systemically (auth gone, source unreachable); orchestrator
    must NOT swallow because that would mark the failed items as
    re-processed when they weren't.
  * ``returns_empty`` — empty iterator when none of the failed item_ids
    are resolvable (every refetch returned tombstone / 404).

F43: every test runs ONE assertion body over the real
:class:`kairix.connectors.slack.SlackConnector` (whose ``reindex``
genuinely refetches each failed id via ``conversations.history``,
driven by the :class:`tests.fakes.FakeSlackWebApi` MockTransport stub)
AND :class:`tests.fakes.FakeResolver`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import Resolver
from tests.fakes import FakeResolver, FakeSlackWebApi

pytestmark = pytest.mark.contract

_FAILED_IDS = ("C0001:1715000000.000100", "C0001:1715000060.000200")

ResolverFactory = Callable[[BaseException | None], Resolver]


def _real(raises: BaseException | None) -> Resolver:
    """Real Slack resolver; a systemic failure is a non-ok ``conversations.history``."""
    if raises is not None:
        api = FakeSlackWebApi(responses={"conversations.history": {"ok": False, "error": "F68_resolver_raises"}})
    else:
        # Every failed message has since been deleted: history returns nothing.
        api = FakeSlackWebApi(responses={"conversations.history": {"messages": []}})
    connector: Resolver = api.build_connector()
    return connector


def _fake(raises: BaseException | None) -> Resolver:
    return FakeResolver(raises=raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_reindex_raises_propagates_typed_exception(factory: ResolverFactory) -> None:
    """A systemic refetch failure surfaces — orchestrator must NOT mark
    the failed items as re-processed when refetch crashed.

    Sabotage proof: in ``kairix.connectors.slack.connector.SlackConnector.reindex``
    widen ``except ContainerAccessDeniedError:`` to ``except Exception:``.
    Re-run: the real leg's pytest.raises sees nothing. Restored.
    """
    res = factory(RuntimeError("slack: conversations.history returned ok=false: 'F68_resolver_raises'"))
    with pytest.raises(RuntimeError, match="F68_resolver_raises"):
        list(res.reindex(_FAILED_IDS))


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_reindex_returns_empty_when_all_failed_ids_now_tombstoned(factory: ResolverFactory) -> None:
    """Empty iterator when every failed id has been tombstoned at the
    source — the orchestrator drops the dead-letter rows without
    re-processing.

    Sabotage proof: in ``SlackConnector.reindex`` yield a synthetic
    ``ChangeEvent(op="deleted", item_id=item_id, ...)`` per failed id
    before the ``conversations.history`` lookup. Re-run: the real leg's
    ``== []`` assertion fails. Restored.
    """
    res = factory(None)
    out = list(res.reindex(_FAILED_IDS))
    assert out == [], f"all-tombstoned must yield []; got {out!r}"
