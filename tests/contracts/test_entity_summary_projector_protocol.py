"""Contract tests for ADR-036 — :class:`EntitySummaryProjector` Protocol.

Every body runs over BOTH the production
:class:`kairix.knowledge.entities.summary_projector.EntitySummaryProjectorImpl`
(Neo4j client, chunk writer and clock injected with fakes) and the
canonical :class:`tests.fakes.FakeEntitySummaryProjector` (F43
behavioural parity):

* both are :func:`isinstance`-compatible with the Protocol
* :meth:`tick` honours its bounded-per-tick contract — the
  ``per_tick_max_items`` each call passes reaches the backend
* the result carries the four counters declared in ADR-036 §Protocol
  (``projected``, ``updated``, ``skipped``, ``failed``), reads zero on
  an idle tick, and is frozen

Failure-injection proofs live in
:mod:`tests.contracts.test_entity_summary_projector_failure_modes`.

F1/F2-clean by construction — every test composes through normal kwargs.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.protocols import EntitySummaryProjectionResult, EntitySummaryProjector
from kairix.knowledge.entities.summary_projector import EntitySummaryProjectorImpl
from tests.fakes import FakeChunkWriter, FakeEntitySummaryProjector, FakeGraphRepository

pytestmark = pytest.mark.contract

_FIXED_TICK = "2026-06-09T00:00:00Z"


@dataclass
class _Harness:
    projector: EntitySummaryProjector
    caps_seen: Callable[[], list[int]]


def _real(pending: int) -> _Harness:
    rows = [
        {
            "name": f"Entity{i}",
            "qid": f"Q{i}",
            "summary": f"summary {i}",
            "prior_hash": "",
            "summary_source": "wikidata",
        }
        for i in range(pending)
    ]
    neo4j = FakeGraphRepository(cypher_rows=rows)
    projector = EntitySummaryProjectorImpl(neo4j=neo4j, chunk_writer=FakeChunkWriter(), clock=lambda: _FIXED_TICK)

    def _caps() -> list[int]:
        # Only the poll query carries the cap bind; the per-entity
        # mark-indexed writes carry entity params.
        return [int(p["per_tick_max_items"]) for _q, p in neo4j.cypher_calls if p and "per_tick_max_items" in p]

    return _Harness(projector=projector, caps_seen=_caps)


def _fake(pending: int) -> _Harness:
    fake = FakeEntitySummaryProjector(result=EntitySummaryProjectionResult(projected=pending))
    return _Harness(projector=fake, caps_seen=lambda: list(fake.ticks))


_IMPLEMENTATIONS: list[tuple[str, Callable[[int], _Harness]]] = [
    ("real", _real),
    ("fake", _fake),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_fake_satisfies_entity_summary_projector_protocol(name: str, factory: Callable[[int], _Harness]) -> None:
    """Both impls pass ``isinstance(x, EntitySummaryProjector)``.

    Locks the Protocol surface — if a future commit drops or renames
    :meth:`tick` on either impl, this assertion fails.
    """
    assert isinstance(factory(0).projector, EntitySummaryProjector), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_projector_tick_returns_protocol_result_shape(name: str, factory: Callable[[int], _Harness]) -> None:
    """:meth:`tick` returns an :class:`EntitySummaryProjectionResult` with all
    four counters present; a tick over 3 pending entities projects 3.

    Sabotage-proof: drop one of the four fields from the dataclass and
    the attribute access here raises ``AttributeError``.
    """
    out: Any = factory(3).projector.tick(per_tick_max_items=50)
    assert isinstance(out, EntitySummaryProjectionResult), name
    assert (out.projected, out.updated, out.skipped, out.failed) == (3, 0, 0, 0), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_projector_tick_records_per_tick_max_items(name: str, factory: Callable[[int], _Harness]) -> None:
    """Every ``per_tick_max_items`` a caller passes reaches the backend —
    the bounded-per-tick contract is plumbed through the Protocol kwarg,
    not silently ignored.

    Sabotage proof (executed): hard-code the poll bind in
    ``EntitySummaryProjectorImpl._fetch_pending`` to ``200`` → the
    ``real`` case sees ``[200, 200]``. Restored.
    """
    h = factory(0)
    h.projector.tick(per_tick_max_items=100)
    h.projector.tick(per_tick_max_items=50)
    assert h.caps_seen() == [100, 50], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_projector_result_defaults_are_zero(name: str, factory: Callable[[int], _Harness]) -> None:
    """An idle tick's result reads zero on every counter — the
    default-safe contract so callers that miss a branch (e.g. flag OFF)
    get a meaningful empty result, not None."""
    result = factory(0).projector.tick(per_tick_max_items=10)
    assert (result.projected, result.updated, result.skipped, result.failed) == (0, 0, 0, 0), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_projector_result_is_frozen(name: str, factory: Callable[[int], _Harness]) -> None:
    """The result each impl returns is frozen — operators can't
    accidentally mutate counters after the projector returns.

    Sabotage-proof: drop ``frozen=True`` from
    :class:`EntitySummaryProjectionResult` and the assignment below
    succeeds instead of raising ``FrozenInstanceError``.
    """
    result = factory(1).projector.tick(per_tick_max_items=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.projected = 99  # type: ignore[misc] — proving FrozenInstanceError; the type-checker reasonably objects so we silence and assert at runtime instead
