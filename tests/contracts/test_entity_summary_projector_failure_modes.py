"""F68 failure-mode contract for :class:`EntitySummaryProjector` (ADR-036).

Protocol-shape proofs live in
:mod:`tests.contracts.test_entity_summary_projector_protocol`. This file
covers the failure classes the F68 detector requires for the Protocol's
one method, :meth:`tick`.

Every body runs over BOTH the production
:class:`kairix.knowledge.entities.summary_projector.EntitySummaryProjectorImpl`
(its Neo4j client, chunk writer and clock injected through constructor
seams with fakes) and the canonical
:class:`tests.fakes.FakeEntitySummaryProjector` (F43 behavioural parity),
each configured to model the same scenario.

F1/F2-clean by construction.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.protocols import EntitySummaryProjectionResult, EntitySummaryProjector
from kairix.knowledge.entities.summary_projector import EntitySummaryProjectorImpl
from tests.fakes import FakeChunkWriter, FakeEntitySummaryProjector, FakeGraphRepository

pytestmark = pytest.mark.contract

_FIXED_TICK = "2026-06-09T00:00:00Z"


def _row(*, name: str, qid: str, summary: str, prior_hash: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "qid": qid,
        "summary": summary,
        "prior_hash": prior_hash,
        "summary_source": "wikidata",
    }


class _FlakyWriter:
    """ChunkWriter whose first upsert fails — per-entity write fault."""

    def __init__(self) -> None:
        self.upsert_calls = 0

    def upsert(self, chunks: Iterable[Any]) -> int:
        self.upsert_calls += 1
        if self.upsert_calls == 1:
            raise RuntimeError("flake on first write")
        return len(list(chunks))

    def delete_by_source_uri(self, _uri: str) -> int:
        return 0


def _raise_clock() -> str:
    raise RuntimeError("simulated tick failure")


@dataclass
class _Harness:
    projector: EntitySummaryProjector
    caps_seen: Callable[[], list[int]]


# Scenarios each impl is configured for.
_IDLE, _NEO4J_DOWN, _PARTIAL, _TICK_FAULT = "idle", "neo4j_down", "partial", "tick_fault"


def _real(scenario: str) -> _Harness:
    neo4j = {
        _IDLE: FakeGraphRepository(cypher_rows=[]),
        _NEO4J_DOWN: FakeGraphRepository(raises=RuntimeError("neo4j-unavailable")),
        _PARTIAL: FakeGraphRepository(
            cypher_rows=[_row(name="Ada", qid="Q1", summary="first"), _row(name="Bob", qid="Q2", summary="second")]
        ),
        _TICK_FAULT: FakeGraphRepository(cypher_rows=[_row(name="Ada", qid="Q1", summary="first")]),
    }[scenario]
    projector = EntitySummaryProjectorImpl(
        neo4j=neo4j,
        chunk_writer=_FlakyWriter() if scenario == _PARTIAL else FakeChunkWriter(),
        clock=_raise_clock if scenario == _TICK_FAULT else (lambda: _FIXED_TICK),
    )
    return _Harness(
        projector=projector,
        caps_seen=lambda: [int((params or {})["per_tick_max_items"]) for _q, params in neo4j.cypher_calls[:1]],
    )


def _fake(scenario: str) -> _Harness:
    fake = {
        _IDLE: FakeEntitySummaryProjector(),
        _NEO4J_DOWN: FakeEntitySummaryProjector(),
        _PARTIAL: FakeEntitySummaryProjector(result=EntitySummaryProjectionResult(projected=1, failed=1)),
        _TICK_FAULT: FakeEntitySummaryProjector(raises=RuntimeError("simulated tick failure")),
    }[scenario]
    return _Harness(projector=fake, caps_seen=lambda: list(fake.ticks))


_IMPLEMENTATIONS: list[tuple[str, Callable[[str], _Harness]]] = [
    ("real", _real),
    ("fake", _fake),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_tick_raises_propagates_to_caller(name: str, factory: Callable[[str], _Harness]) -> None:
    """F68 ``raises`` — a TICK-level fault (here the injected clock the
    whole tick stamps its chunks with) propagates so the worker boundary
    can absorb-or-surface deliberately. Per-entity faults are a
    different class (counted in ``failed``, see the partial test).

    Sabotage proof (executed): in ``EntitySummaryProjectorImpl.tick`` wrap
    ``tick_iso = str(self._clock())`` in ``try/except Exception:
    return EntitySummaryProjectionResult()``. Re-run: the ``real`` case
    fails because no exception reaches the caller. Restored.
    """
    with pytest.raises(RuntimeError, match="simulated tick failure"):
        factory(_TICK_FAULT).projector.tick(per_tick_max_items=50)


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_tick_returns_empty_for_no_pending_entities(name: str, factory: Callable[[str], _Harness]) -> None:
    """F68 ``returns_empty`` — when Neo4j has nothing to project, the
    projector returns an all-zero :class:`EntitySummaryProjectionResult`,
    NOT None. The worker telemetry records an idle tick the same way as
    a productive one.

    Sabotage proof: in ``EntitySummaryProjectorImpl.tick`` change the
    ``if not rows: return EntitySummaryProjectionResult()`` to
    ``return None``. Re-run: the ``real`` case fails. Restored.
    """
    out = factory(_IDLE).projector.tick(per_tick_max_items=100)
    assert isinstance(out, EntitySummaryProjectionResult), name
    assert (out.projected, out.updated, out.skipped, out.failed) == (0, 0, 0, 0), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_tick_returns_empty_when_neo4j_unavailable(name: str, factory: Callable[[str], _Harness]) -> None:
    """F68 ``unavailable`` — the Neo4j poll raises → the projector returns
    an all-zero result, never propagates (absorb-at-poll contract).

    Sabotage proof (executed): drop the ``try/except`` around the cypher
    call in ``_fetch_pending`` → the ``real`` case raises RuntimeError.
    Restored.
    """
    out = factory(_NEO4J_DOWN).projector.tick(per_tick_max_items=10)
    assert (out.projected, out.updated, out.skipped, out.failed) == (0, 0, 0, 0), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_tick_returns_partial_when_chunk_writer_raises_on_some_entities(
    name: str, factory: Callable[[str], _Harness]
) -> None:
    """F68 ``returns_partial`` — a per-entity ChunkWriter failure is
    counted in ``failed``; the remaining entities still project (ADR-036
    §Expected behaviours #6 failure isolation).

    Sabotage proof: in ``EntitySummaryProjectorImpl.tick`` remove the
    per-entity ``try/except`` → the ``real`` case raises instead.
    Restored.
    """
    out = factory(_PARTIAL).projector.tick(per_tick_max_items=10)
    assert out.projected == 1, name
    assert out.failed == 1, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_tick_returns_partial_respects_per_tick_max_items_cap(name: str, factory: Callable[[str], _Harness]) -> None:
    """F68 ``returns_partial`` (cap variant) — ``per_tick_max_items``
    reaches the backend (the real projector forwards it into the Cypher
    LIMIT bind; the fake records it).

    Sabotage proof: hard-code ``per_tick_max_items`` to 200 in
    ``_fetch_pending`` → the ``real`` case sees the wrong LIMIT bind.
    Restored.
    """
    h = factory(_IDLE)
    h.projector.tick(per_tick_max_items=42)
    assert h.caps_seen() == [42], name
