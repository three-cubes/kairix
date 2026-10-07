"""GH #334 — Protocol shape for the graph backend the drain talks to.

Keeping a narrow Protocol here (rather than importing the full
:class:`kairix.knowledge.graph.repository.Neo4jGraphRepository`) means
the drain module can be unit-tested with a 3-line fake; it also
satisfies F26 (kairix/core/** must not import kairix/providers/** or
kairix/transport/**; the narrow Protocol is the only thing the drain
sees of the graph backend).

The production repository
:class:`~kairix.knowledge.graph.repository.Neo4jGraphRepository`
already exposes ``available`` + ``cypher(query, params)`` so it
satisfies this Protocol structurally (PEP-544); no inheritance is
required.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DrainGraphRepository(Protocol):
    """Minimal graph-backend surface the Neo4j drain depends on.

    Only two members are needed:

      * ``available`` — boolean; when ``False`` the drain treats the
        backend as unreachable and skips the tick.
      * ``cypher(query, params)`` — executes a Cypher MERGE and RAISES
        when the backend rejects it; rows returned are ignored (the drain
        doesn't read them). A swallowed failure would be acknowledged as
        pushed and the signal lost.

    Implementations:
      * Production:
        :class:`kairix.knowledge.graph.repository.Neo4jGraphRepository`.
      * Tests: :class:`tests.fakes.FakeDrainGraphRepository` — records
        every cypher call so assertions can verify what landed.
    """

    @property
    def available(self) -> bool:
        """True when the graph backend is reachable; False when degraded or offline."""
        ...

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run a Cypher MERGE; raise on failure; return rows (drain ignores)."""
        ...
