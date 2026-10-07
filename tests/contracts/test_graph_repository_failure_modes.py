"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`GraphRepository`.

Every public method on :class:`kairix.core.protocols.GraphRepository`
has at least one test here that exercises a named failure class
(``raises`` / ``returns_empty`` / ``unavailable``).

The Neo4j-backed graph is an optional collaborator — every method
must absorb-or-surface its failure cleanly so the SearchPipeline keeps
serving chunk-only results when the graph is down (Bug-class: a
single Neo4j hiccup must not nuke a search response).

F43 parity: every body runs over BOTH the real
:class:`kairix.knowledge.graph.repository.Neo4jGraphRepository` (composed
over a real :class:`kairix.knowledge.graph.client.Neo4jClient` whose
driver is injected through the public ``driver_cls=`` seam with
:class:`tests.fakes.FakeNeo4jDriverClass`) AND the canonical
:class:`tests.fakes.FakeGraphRepository`.

Finding (fake-vs-real drift): the real ``Neo4jClient.cypher`` SWALLOWS a
backend error (logs + returns ``[]``) while ``FakeGraphRepository(raises=)``
raises. The raising knob is kept for the caller-robustness tests that use
it; the parity body below drives the fake through its
``swallow_errors=True`` knob, which mirrors the real contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import GraphRepository
from kairix.knowledge.graph.client import Neo4jClient
from kairix.knowledge.graph.repository import Neo4jGraphRepository
from tests.fakes import FakeGraphRepository, FakeNeo4jDriverClass

pytestmark = pytest.mark.contract

_IDS = ["real", "fake"]

_ENTITY_ROWS: list[dict[str, Any]] = [
    {
        "id": "person-agent-alpha",
        "label": "Person",
        "name": "agent-alpha",
        "vault_path": "people/agent-alpha.md",
        "summary": "",
    }
]


def _name_filtered(rows: list[dict[str, Any]]) -> Callable[[str, dict[str, Any]], list[dict[str, Any]]]:
    """Driver responder modelling Neo4j's case-insensitive ``$name`` match."""

    def _respond(_query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        name = params.get("name")
        if name is None:
            return list(rows)
        return [r for r in rows if str(r["name"]).lower() == str(name).lower()]

    return _respond


def _real_repo(**driver_kwargs: Any) -> GraphRepository:
    """Real Neo4jGraphRepository over a real Neo4jClient with a fake driver."""
    client = Neo4jClient(
        uri="bolt://test:7687",
        user="test",
        password="test",  # pragma: allowlist secret
        driver_cls=FakeNeo4jDriverClass(**driver_kwargs),
    )
    return Neo4jGraphRepository(client)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real_repo(connect_raises=ConnectionError("neo4j unreachable")),
        lambda: FakeGraphRepository(available=False),
    ],
    ids=_IDS,
)
def test_available_returns_empty_when_backend_unavailable(factory: Callable[[], GraphRepository]) -> None:
    """The ``available`` property is the gate every caller checks before
    routing to ``cypher`` / ``find_entity``. The "returns_empty" failure
    class for a bool is ``False`` — and every caller honours it.

    Sabotage proof: in ``Neo4jClient._connect`` set ``self.available = True``
    before ``verify_connectivity()``. Re-ran: the real leg fails because
    the assertion expects False. Restored.
    """
    repo = factory()
    assert repo.available is False


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real_repo(responder=_name_filtered(_ENTITY_ROWS)),
        lambda: FakeGraphRepository(entities=_ENTITY_ROWS),
    ],
    ids=_IDS,
)
def test_find_entity_returns_empty_when_name_absent(factory: Callable[[], GraphRepository]) -> None:
    """Unknown entity-name lookup returns ``None`` (the "returns_empty"
    shape) so callers can distinguish "no entity" from "lookup errored".

    Sabotage proof: in ``Neo4jGraphRepository.find_entity`` change
    ``return results[0] if results else None`` to ``... else {}``.
    Re-ran: the real leg's ``is None`` assertion fails. Restored.
    """
    repo = factory()
    assert repo.find_entity("agent-zeta") is None
    # Hit path still works — proves the lookup ran (not a global None
    # short-circuit that would mask everything).
    hit = repo.find_entity("AGENT-ALPHA")
    assert hit is not None
    assert hit["name"] == "agent-alpha"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real_repo(rows=[]),
        lambda: FakeGraphRepository(entities=[]),
    ],
    ids=_IDS,
)
def test_entity_in_degrees_returns_empty_when_graph_empty(factory: Callable[[], GraphRepository]) -> None:
    """An empty graph returns an empty list, not ``None`` — callers
    iterate the result without a null check.

    Sabotage proof: in ``Neo4jClient.cypher`` change the success return to
    ``return [dict(r) for r in result] or None``. Re-ran: the real leg's
    ``== []`` assertion fails (None != []). Restored.
    """
    repo = factory()
    assert repo.entity_in_degrees() == []


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real_repo(run_raises=RuntimeError("F68-cypher-raises")),
        lambda: FakeGraphRepository(raises=RuntimeError("F68-cypher-raises"), swallow_errors=True),
    ],
    ids=_IDS,
)
def test_cypher_raises_swallowed_returns_empty_on_backend_error(factory: Callable[[], GraphRepository]) -> None:
    """When the Neo4j backend raises (network blip, query error) the real
    ``Neo4jClient.cypher`` logs and returns ``[]`` — the graph is an
    optional collaborator, so a backend failure degrades to "no rows"
    instead of propagating into the search response.

    Sabotage proof: in ``Neo4jClient.cypher`` replace the
    ``except Exception`` fallback ``return []`` with ``raise``. Re-ran: the
    real leg raises RuntimeError and fails. Restored.
    """
    repo = factory()
    assert repo.cypher("MATCH (n) RETURN n") == []


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _real_repo(rows=[]),
        lambda: FakeGraphRepository(entities=[], cypher_rows=[]),
    ],
    ids=_IDS,
)
def test_cypher_returns_empty_when_no_rows_match(factory: Callable[[], GraphRepository]) -> None:
    """No-match query returns ``[]`` — callers can branch on emptiness
    without a try/except.

    Sabotage proof: in ``Neo4jGraphRepository.cypher`` change the body to
    ``return self._client.cypher(query, params) or None``. Re-ran: the real
    leg's ``== []`` fails. Restored.
    """
    repo = factory()
    assert repo.cypher("MATCH () RETURN 1") == []
