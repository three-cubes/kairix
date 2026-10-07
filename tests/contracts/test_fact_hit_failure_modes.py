"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FactHit`.

``FactHit`` is a thin search-result wrapper — two ``@property``
accessors (``record`` + ``score``). The Protocol's documented failure
surface is **returns_empty** for the score (0.0 = floor / no recall
signal) and **returns_empty** for the record reference (None-shaped
hits are not allowed by the contract — the hit ALWAYS carries a
record). Both shapes are pinned below.

Every body runs over BOTH the production
:class:`kairix.core.facts.store.StoredFactHit` (what
``SQLiteFactStore.search`` returns) and the canonical
:class:`tests.fakes.FakeFactHit` (F43 behavioural parity).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.facts.store import StoredFactHit
from kairix.core.protocols import FactHit
from tests.fakes import FakeFactHit, FakeFactRecord

pytestmark = pytest.mark.contract

HitFactory = Callable[[Any, float], FactHit]

_IMPLEMENTATIONS: list[tuple[str, HitFactory]] = [
    ("real", lambda record, score: StoredFactHit(record=record, score=score)),
    ("fake", lambda record, score: FakeFactHit(record=record, score=score)),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_record_returns_empty_when_no_match_record_provided(name: str, factory: HitFactory) -> None:
    """The ``record`` accessor surfaces the underlying FactRecord
    verbatim. The "empty" boundary shape is a record with empty-string
    fields — the Protocol does NOT allow ``record`` to be None.

    Sabotage proof: change ``StoredFactHit.record`` to ``return None``.
    Re-run: the ``real`` case's ``hit.record.id`` raises AttributeError.
    Restored.
    """
    empty = FakeFactRecord(id="", entity="", attribute="", value="")
    hit = factory(empty, 0.0)
    assert hit.record.id == "", name
    assert hit.record is empty, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_score_returns_empty_when_zero_relevance(name: str, factory: HitFactory) -> None:
    """A score of 0.0 is the "no recall signal" floor — distinguishable
    from negative scores (forbidden by contract) and from a
    None-shaped score (also forbidden — the read accessor must
    always return a float).

    Sabotage proof (executed): change ``StoredFactHit.score`` to
    ``return self._score or 0.5``. Re-run: the ``real`` case's ``== 0.0``
    fails. Restored.
    """
    record = FakeFactRecord(id="f1", entity="x", attribute="y", value="z")
    hit = factory(record, 0.0)
    assert hit.score == 0.0, name
    assert isinstance(hit.score, float), name
