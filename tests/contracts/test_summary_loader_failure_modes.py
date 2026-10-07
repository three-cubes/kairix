"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SummaryLoader`.

``SummaryLoader.get_l0`` / ``get_l1`` feed the token-budget enforcer
(:func:`kairix.core.search.budget.apply_budget`) the L0 abstract / L1
overview for a hit. When the summaries store is broken (schema missing)
or absent, the enforcer must still return the hit — falling back to the
frontmatter-stripped snippet at the selected tier — rather than dropping
the result or returning an empty context to the agent.

One body per method, two implementations (F43 limb 2):

* a loader over the production summary accessors
  (:func:`kairix.knowledge.summaries.loader.get_l0` / ``get_l1`` — the exact
  calls the Phase-2 default loader makes) bound to an un-migrated SQLite DB
  (no ``summaries`` table) or to no DB at all. The default loader itself is
  module-private and has no DB seam, so it is not importable here (F5);
* the canonical :class:`tests.fakes.FakeSummaryLoader` raising the same
  ``sqlite3.OperationalError`` / holding no summaries.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.search.budget import apply_budget
from kairix.core.search.rrf import FusedResult
from kairix.knowledge.summaries.loader import get_l0, get_l1
from tests.fakes import FakeSummaryLoader

pytestmark = pytest.mark.contract

_SNIPPET = "---\ntitle: Deploy runbook\n---\nRoll the worker first, then the MCP server."
_STRIPPED = "Roll the worker first, then the MCP server."


def _hit(score: float) -> FusedResult:
    return FusedResult(
        path="ops/deploy-runbook.md",
        collection="shared",
        title="Deploy runbook",
        snippet=_SNIPPET,
        rrf_score=score,
        boosted_score=score,
    )


class _SqliteSummaryLoader:
    """``SummaryLoader`` over the production summary accessors.

    Mirrors the Phase-2 default loader: ``None`` when no summaries DB is
    available, otherwise delegate to ``get_l0`` / ``get_l1`` on the DB.
    """

    def __init__(self, db: sqlite3.Connection | None) -> None:
        self._db = db

    def get_l0(self, path: str) -> str | None:
        return None if self._db is None else get_l0(path, self._db)

    def get_l1(self, path: str) -> str | None:
        return None if self._db is None else get_l1(path, self._db)


_BROKEN_FACTORIES: list[Callable[[], Any]] = [
    # A summaries DB that exists but was never migrated (no table).
    lambda: _SqliteSummaryLoader(sqlite3.connect(":memory:")),
    lambda: FakeSummaryLoader(raises=sqlite3.OperationalError("no such table: summaries")),
]
_MISSING_FACTORIES: list[Callable[[], Any]] = [
    lambda: _SqliteSummaryLoader(None),
    lambda: FakeSummaryLoader(),
]
_IDS = ["sqlite-summary-accessors", "fake"]


@pytest.mark.parametrize("factory", _BROKEN_FACTORIES, ids=_IDS)
def test_get_l0_raises_on_unmigrated_store_budget_serves_stripped_snippet(factory: Callable[[], Any]) -> None:
    """A low-score hit selects tier L0; the loader raises, and the enforcer
    still returns the hit with the frontmatter-stripped snippet.

    Sabotage proof: in ``kairix/core/search/budget.py::_lookup_tier_summary``
    replace the ``except Exception`` body with ``raise``. Re-run: both cases
    fail because ``apply_budget``'s outer guard swallows the error into an
    empty list (the hit is dropped). Restored.
    """
    loader = factory()
    with pytest.raises(sqlite3.OperationalError, match="summaries"):
        loader.get_l0("ops/deploy-runbook.md")

    budgeted = apply_budget([_hit(0.01)], budget=3000, summary_loader=loader)

    assert [(b.result.path, b.tier, b.content) for b in budgeted] == [("ops/deploy-runbook.md", "L0", _STRIPPED)]


@pytest.mark.parametrize("factory", _MISSING_FACTORIES, ids=_IDS)
def test_get_l1_returns_empty_when_store_absent_budget_serves_stripped_snippet(factory: Callable[[], Any]) -> None:
    """A mid-score hit selects tier L1; with no summaries DB the loader
    returns ``None`` for both L1 and the L0 fallback, and the enforcer
    serves the stripped snippet at tier L1.

    Sabotage proof: in ``_get_content_for_tier`` change the final fallback
    to ``return ""``. Re-run: both cases fail because the hit's content is
    empty (and its token estimate 0). Restored.
    """
    loader = factory()
    assert loader.get_l1("ops/deploy-runbook.md") is None

    budgeted = apply_budget([_hit(0.20)], budget=1000, summary_loader=loader)

    assert [(b.tier, b.content) for b in budgeted] == [("L1", _STRIPPED)]
    assert budgeted[0].token_estimate > 0
