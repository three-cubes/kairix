"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FactRecord`.

``FactRecord`` is a pure-read Protocol (every method is a ``@property``
accessor). The Protocol's documented "failure" surface is the
**returns_empty** path for the two nullable fields (``superseded_by``
and ``evidence_at``) plus the empty-tuple shape for ``source_turn_ids``
on legacy / invalid rows.

Per F68 spec — "When the Protocol method's failure surface is
genuinely empty (rare — a pure-functional method with no I/O)":
``test_<method>_returns_empty_when_no_input_provided`` is the
canonical name.

Every body runs over BOTH the production
:class:`kairix.core.facts.records.StoredFactRecord` (what
``SQLiteFactStore`` persists and reads back) and the canonical
:class:`tests.fakes.FakeFactRecord` (F43 behavioural parity). The
"bare" / legacy-row shape is built from the required fields only on the
fake (its defaults) and from the legacy-row column values on the real
record (which has no defaults) — so the same assertion proves the fake's
defaults match what a legacy row actually carries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.facts.records import StoredFactRecord
from kairix.core.protocols import FactRecord
from tests.fakes import FakeFactRecord

pytestmark = pytest.mark.contract

# The column values a legacy (pre-Lever-A, unscoped, never-superseded)
# ``facts`` row carries for the optional fields.
_LEGACY_ROW: dict[str, Any] = {
    "confidence": 0.9,
    "source_turn_ids": (),
    "extracted_at": "1970-01-01T00:00:00Z",
    "superseded_by": None,
    "namespace": "shared",
}

RecordFactory = Callable[..., FactRecord]


def _real(**fields: Any) -> FactRecord:
    return StoredFactRecord(**{**_LEGACY_ROW, **fields})


def _fake(**fields: Any) -> FactRecord:
    return FakeFactRecord(**fields)


_IMPLEMENTATIONS: list[tuple[str, RecordFactory]] = [
    ("real", _real),
    ("fake", _fake),
]


def _bare(factory: RecordFactory) -> FactRecord:
    """A FactRecord with only the required fields supplied (legacy row)."""
    return factory(id="f1", entity="agent-alpha", attribute="role", value="VP")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_id_returns_empty_when_no_input_provided(name: str, factory: RecordFactory) -> None:
    """The ``id`` accessor returns the supplied id verbatim — no
    transformation, no derivation. The "empty" failure shape is the empty
    string, which is a valid (but discouraged) id.

    Sabotage proof: make ``StoredFactRecord`` derive ``id`` via
    ``__post_init__`` (``mint_id``) when empty. Re-run: the ``real`` case
    fails. Restored.
    """
    assert factory(id="", entity="x", attribute="y", value="z").id == "", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_entity_returns_empty_when_no_input_provided(name: str, factory: RecordFactory) -> None:
    """The ``entity`` accessor surfaces the entity string verbatim."""
    assert factory(id="f1", entity="", attribute="y", value="z").entity == "", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_attribute_returns_empty_when_no_input_provided(name: str, factory: RecordFactory) -> None:
    """The ``attribute`` accessor surfaces the attribute string verbatim."""
    assert factory(id="f1", entity="x", attribute="", value="z").attribute == "", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_value_returns_empty_when_no_input_provided(name: str, factory: RecordFactory) -> None:
    """The ``value`` accessor surfaces the value string verbatim."""
    assert factory(id="f1", entity="x", attribute="y", value="").value == "", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_confidence_returns_empty_when_zero_provided(name: str, factory: RecordFactory) -> None:
    """``confidence`` is a float in [0.0, 1.0]; the boundary "empty" shape
    is 0.0 — pinning a fact the extractor was certain was wrong.

    Sabotage proof: change ``FakeFactRecord.confidence`` to clamp 0.0 to
    0.5. Re-run: the ``fake`` case's ``== 0.0`` fails. Restored.
    """
    assert factory(id="f1", entity="x", attribute="y", value="z", confidence=0.0).confidence == 0.0, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_source_turn_ids_returns_empty_when_no_input_provided(name: str, factory: RecordFactory) -> None:
    """Legacy rows pre-Lever-A have empty ``source_turn_ids`` tuples — the
    read accessor returns the empty tuple shape verbatim.

    Sabotage proof: change ``FakeFactRecord.source_turn_ids`` to return
    ``(None,)`` for empty. Re-run: the ``fake`` case fails. Restored.
    """
    assert _bare(factory).source_turn_ids == (), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extracted_at_returns_empty_when_epoch_provided(name: str, factory: RecordFactory) -> None:
    """A legacy row's ``extracted_at`` is the epoch sentinel — pinning a
    "no real timestamp" row shape that callers can detect."""
    assert _bare(factory).extracted_at == "1970-01-01T00:00:00Z", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_superseded_by_returns_empty_when_record_is_live(name: str, factory: RecordFactory) -> None:
    """Live (current) facts have ``superseded_by is None`` — the canonical
    "returns_empty" shape. Callers test ``is None`` to filter superseded
    rows out of default search.

    Sabotage proof: change ``FakeFactRecord.superseded_by`` to return an
    empty string. Re-run: the ``fake`` case's ``is None`` fails. Restored.
    """
    assert _bare(factory).superseded_by is None, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_namespace_returns_empty_when_default_provided(name: str, factory: RecordFactory) -> None:
    """``namespace`` is ``"shared"`` on a legacy row — the engagement-scope-
    free shape every fact carries until explicit scoping kicks in."""
    assert _bare(factory).namespace == "shared", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_evidence_at_returns_empty_when_no_temporal_anchor(name: str, factory: RecordFactory) -> None:
    """Pre-Lever-A legacy rows carry ``evidence_at is None`` — the "no
    event-time anchor" shape. Production code uses this exact null-check
    to decide whether to fall back to ``extracted_at``.

    Sabotage proof (executed): change ``StoredFactRecord.evidence_at``'s
    default to ``""``. Re-run: the ``real`` case's ``is None`` fails.
    Restored.
    """
    assert _bare(factory).evidence_at is None, name
