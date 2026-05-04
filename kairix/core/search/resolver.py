"""Default CollectionResolver — composes config + scope into a collection list.

Replaces the historical ``_collections_for`` private helper in hybrid.py
which read module-level state and env vars on every call. The Adapter
takes the loaded CollectionsConfig + extra-collections + AgentRegistry at
construction time (G4: config at boundary), so call sites depend on the
Protocol surface and tests inject ``FakeCollectionResolver`` from
``tests/fakes`` rather than reaching into private module state.

Closes the TEST-INFRA-AUDIT #6 private-import debt and implements
KFEAT-GAP-8 ``scope=all-agents`` semantics via the injected
AgentRegistry (WS3-3).
"""

from __future__ import annotations

from typing import Any

from kairix.core.search.config_loader import CollectionsConfig
from kairix.core.search.scope import Scope


class DefaultCollectionResolver:
    """Production CollectionResolver Adapter.

    Lifecycle:
      - Construct once at startup with the loaded CollectionsConfig (or None
        if no kairix.config.yaml is present) plus any operator-supplied
        extras (e.g. via KAIRIX_EXTRA_COLLECTIONS, resolved at the boundary).
      - Pass the instance into SearchPipeline (or any other caller) as a
        CollectionResolver.

    Scope semantics (matches the historical _collections_for behaviour for
    SHARED, AGENT, SHARED_AGENT — the three values the existing code path
    supported — and explicitly raises NotImplementedError for ALL_AGENTS
    and EVERYTHING which need an AgentRegistry to know which agents exist):

      SHARED        — only the shared collections (no agent appended)
      AGENT         — only the agent's own collection (no shared)
      SHARED_AGENT  — shared collections plus the agent's collection
      ALL_AGENTS    — every agent's collection (no shared) — needs AgentRegistry
      EVERYTHING    — shared + every agent's collection — needs AgentRegistry
    """

    _DEFAULT_AGENT_PATTERN = "{agent}-memory"

    def __init__(
        self,
        *,
        collections_config: CollectionsConfig | None,
        extra_collections: list[str] | None = None,
        agent_registry: Any | None = None,
    ) -> None:
        self._config = collections_config
        self._extra: list[str] = list(extra_collections or [])
        self._registry = agent_registry

    def resolve(self, agent: str | None, scope: object) -> list[str] | None:
        # Accept Scope or any string-convertible scope value (Scope subclasses str,
        # and historical callers may still pass plain strings during the migration
        # period). Coerce to Scope — Scope.parse raises on truly unknown values
        # which is the correct signal.
        scope_enum = scope if isinstance(scope, Scope) else Scope.parse(str(scope))

        pattern = self._config.agent_pattern if self._config else self._DEFAULT_AGENT_PATTERN

        if scope_enum is Scope.SHARED:
            cols = self._shared_collections()
        elif scope_enum is Scope.AGENT:
            if not agent:
                return None  # No agent → no scope filter
            cols = [pattern.format(agent=agent)]
        elif scope_enum is Scope.SHARED_AGENT:
            cols = list(self._shared_collections())
            if agent:
                cols.append(pattern.format(agent=agent))
        elif scope_enum is Scope.ALL_AGENTS:
            cols = self._all_agent_collections()
        elif scope_enum is Scope.EVERYTHING:
            cols = list(self._shared_collections()) + self._all_agent_collections()
        else:  # pragma: no cover — defensive; Scope.parse rejects unknowns
            cols = []

        return cols or None

    def _shared_collections(self) -> list[str]:
        cols: list[str] = []
        if self._config:
            cols.extend(c.name for c in self._config.shared)
        cols.extend(self._extra)
        return cols

    def _all_agent_collections(self) -> list[str]:
        """Concrete agent collections from the AgentRegistry.

        Raises NotImplementedError when no registry is configured — the
        misconfiguration is loud rather than silent (returns empty list →
        "search nothing" would mask bad ops).
        """
        if self._registry is None:
            raise NotImplementedError(
                "scope=all-agents / everything requires an AgentRegistry. "
                "Configure agents: in kairix.config.yaml or pass agent_registry "
                "to DefaultCollectionResolver."
            )
        return [agent.collection for agent in self._registry.list_agents()]
