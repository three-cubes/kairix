"""Search-client transport seam — pure documentation contract today.

The probe drives a search backend through a callable. Today there's exactly
one production implementation, ``InProcessSearchClient``, which calls
``build_search_pipeline()`` in the probe's own process. Tomorrow's PVT
harness will add ``MCPHttpSearchClient`` (#284) — same Protocol, real MCP
JSON-RPC over HTTP — so the probe code, CLI, and per-category stats logic
stay identical across transports.

Why this matters architecturally. The probe is the measurement instrument;
WHICH transport it measures decides which question gets answered:

- ``InProcessSearchClient`` measures the **Python-pipeline regression surface**:
  factory build, fusion logic, percentile calc, intent classification. CLI
  subprocess pays cold-start tax (~4-5s); not what an agent experiences.
- ``MCPHttpSearchClient`` will measure **agent-experienced latency**: real
  MCP framing, real warm-server pipeline, real client-side stopwatch. This
  is the PVT measurement (see docs/architecture/performance-testing-approach.md).

The probe's ``searcher`` kwarg accepts any ``Callable[[SampledQuery], Any]``.
The Protocol below names the contract and exists for documentation +
``isinstance`` checks; the runtime kwarg type stays Callable so a bare
lambda still works in tests, and so future client classes drop in by
passing ``client.search`` (a bound method, which IS a Callable).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from kairix.quality.probe.runner import SampledQuery


@runtime_checkable
class SearchClient(Protocol):
    """The contract probes need from any search backend.

    Implementations:
      - :class:`InProcessSearchClient` — production, in-process kairix pipeline
      - ``MCPHttpSearchClient`` (future, #284) — real MCP JSON-RPC client

    Structural typing: any object with ``search(SampledQuery) -> Any``
    satisfies the protocol. Tests usually pass a bare callable to the
    probe's ``searcher=`` kwarg, which IS Callable-shaped, not Protocol-
    shaped — both forms are accepted.
    """

    def search(self, query: SampledQuery) -> Any:
        """Execute one search for the sampled query; return the result envelope."""


class InProcessSearchClient:
    """Drives a real kairix search pipeline in the probe's own process.

    Uses the memoised factory (``build_search_pipeline``) so successive
    calls share one pipeline instance. The first call in a fresh subprocess
    pays the factory-build cost (~2-3s); subsequent calls are warm.

    Important caveat: this is the **Python-pipeline regression** measurement,
    not the agent-experienced measurement. Agents over MCP talk to a
    long-running already-warm server; the CLI subprocess shape used by
    ``kairix probe search`` doesn't reflect that. For agent-experienced
    numbers use the PVT layer (docs/architecture/performance-testing-approach.md).
    """

    def search(self, query: SampledQuery) -> Any:  # pragma: no cover — production path
        from kairix.core.factory import build_search_pipeline

        pipeline = build_search_pipeline()
        return pipeline.search(query=query.query, agent=query.agent)
