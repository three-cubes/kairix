"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`BriefingSourceProtocol`.

``kairix.quality.contracts.briefing.BriefingSourceProtocol`` is the
quality-layer contract for a source that contributes briefing items for
an agent. No production class declares it directly — the production
briefing sources are plain functions returning a text section — so the
contract is proved over a thin conforming adapter around the real
knowledge-rules source (:func:`kairix.agents.briefing.sources.fetch_knowledge_rules`).

When the backing store is unreadable the source must contribute *no
items* (``[]``) so the briefing still renders from its other sources —
never raise into the briefing pipeline or emit an empty-bodied item.

One body, two implementations (F43 limb 2):

* the adapter over the real rules source whose ``rules.md`` paths exist
  but cannot be read (they are directories);
* the canonical :class:`tests.fakes.FakeBriefingSource` with no items.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.agents.briefing.sources import fetch_knowledge_rules
from kairix.quality.contracts.briefing import BriefingSourceProtocol
from tests.fakes import FakeBriefingSource

pytestmark = pytest.mark.contract


class _RulesBriefingSource:
    """``BriefingSourceProtocol`` over the production knowledge-rules source."""

    def __init__(self, document_root: Path) -> None:
        self._document_root = document_root

    def fetch(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        body = fetch_knowledge_rules(agent, document_root=self._document_root)
        items = [{"title": f"Rules for {agent}", "body": body}] if body else []
        return items[:limit]


def _real_unreadable(tmp_path: Path) -> Any:
    # Both rules.md candidates exist but are directories: exists() is True,
    # read_text() raises IsADirectoryError — an unreadable knowledge store.
    for owner in ("agent-alpha", "shared"):
        (tmp_path / "04-Agent-Knowledge" / owner / "rules.md").mkdir(parents=True)
    return _RulesBriefingSource(tmp_path)


def _fake_empty(_tmp_path: Path) -> Any:
    return FakeBriefingSource(items=[])


_FACTORIES: list[Callable[[Path], Any]] = [_real_unreadable, _fake_empty]


@pytest.mark.parametrize("factory", _FACTORIES, ids=["real-rules-source", "fake"])
def test_fetch_returns_empty_when_backing_store_unreadable(factory: Callable[[Path], Any], tmp_path: Path) -> None:
    """An unreadable store contributes no briefing items — not an exception,
    not an item with an empty body.

    Sabotage proof: in ``kairix/agents/briefing/sources.py::_read_rules_file``
    change the ``except Exception`` body's ``return None`` to return the
    bare section header (``f"### Rules from {path.parent.name}/rules.md"``).
    Re-run: the real case fails because the source now yields an item whose
    body is a header with no rules. Restored.
    """
    source = factory(tmp_path)
    assert isinstance(source, BriefingSourceProtocol)

    assert source.fetch("agent-alpha") == []
    assert source.fetch("agent-alpha", limit=1) == []
