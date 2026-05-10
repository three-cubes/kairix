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

Default-scope membership is governed by ``CollectionsConfig`` itself,
not by a hardcoded reserved set in this module. Operators control which
collections participate in default search via the ``in_default: bool``
flag on each collection in ``kairix.config.yaml``. Reflib auto-injection
(handled by ``kairix/core/embed/cli.py``) creates a structural reserve
on the agent-side, enforced at registry parse time in ``registry.py``.
"""

from __future__ import annotations

import logging
from typing import Any

from kairix.core.search.config_loader import CollectionsConfig
from kairix.core.search.scope import Scope

logger = logging.getLogger(__name__)


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    """Return ``items`` with duplicates removed, keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


class DefaultCollectionResolver:
    """Production CollectionResolver Adapter.

    Lifecycle:
      - Construct once at startup with the loaded CollectionsConfig (or None
        if no kairix.config.yaml is present) plus any operator-supplied
        extras (e.g. via KAIRIX_EXTRA_COLLECTIONS, resolved at the boundary).
      - Pass the instance into SearchPipeline (or any other caller) as a
        CollectionResolver.

    Scope semantics:

      SHARED        — only the default-eligible shared collections
      AGENT         — only the agent's own collections
      SHARED_AGENT  — default-eligible shared plus the agent's collections
      ALL_AGENTS    — every agent's collections (no shared) — needs AgentRegistry
      EVERYTHING    — default-eligible shared + every agent's collections — needs AgentRegistry
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

        match scope_enum:
            case Scope.SHARED:
                cols = self._default_shared()
            case Scope.AGENT:
                cols = self._collections_for_agent(agent)
            case Scope.SHARED_AGENT:
                cols = self._default_shared() + self._collections_for_agent(agent)
            case Scope.ALL_AGENTS:
                cols = self._all_agent_collections()
            case Scope.EVERYTHING:
                cols = _dedupe_preserving_order(self._default_shared() + self._all_agent_collections())

        return cols or None

    # ------------------------------------------------------------------
    # Helpers — each one has a single responsibility and reads from the
    # data class predicates rather than reaching into config internals.
    # ------------------------------------------------------------------

    def _default_shared(self) -> list[str]:
        """Default-scope shared collections plus operator extras."""
        cols: list[str] = []
        if self._config is not None:
            cols.extend(self._config.default_collection_names())
        cols.extend(self._extra)
        return cols

    def _collections_for_agent(self, agent: str | None) -> list[str]:
        """Resolve the agent's read collections.

        With a registry, returns the agent's full multi-path collection list.
        Without a registry (legacy deployments), falls back to the historical
        single-collection ``pattern.format(agent=agent)`` shape so the search
        contract stays backwards-compatible.
        """
        if not agent:
            return []
        if self._registry is not None:
            try:
                return list(self._registry.collections_for(agent))
            except KeyError:
                # Agent not registered — fall through to pattern fallback.
                logger.debug("resolver: agent %r not in registry, using legacy pattern", agent)
        pattern = self._config.agent_pattern if self._config else self._DEFAULT_AGENT_PATTERN
        return [pattern.format(agent=agent)]

    def _all_agent_collections(self) -> list[str]:
        """Concrete agent collections from the AgentRegistry.

        Returns the dedup-union of every agent's ``collection_names()`` so
        cross-agent shared paths (e.g. ``04-Agent-Knowledge/shared``) are
        never duplicated in the resolver's output.

        Raises ``NotImplementedError`` when no registry is configured *or*
        when the registry has zero agents — the misconfiguration is loud
        rather than silent (returning an empty list would mask bad ops as
        "search nothing", which downstream backends interpret as "no filter
        — search everything", silently returning the wrong content).
        """
        if self._registry is None:
            raise NotImplementedError(
                "scope=all-agents / scope=everything requires an AgentRegistry. "
                "Add an `agents:` section to kairix.config.yaml — minimum: "
                "`agents: [{name: <agent>, write_path: <path>}]` — or pass "
                "agent_registry= to DefaultCollectionResolver in the factory."
            )
        # Prefer the registry-level method when available (production registry);
        # fall back to per-agent iteration for fakes that only implement
        # ``list_agents()``.
        if hasattr(self._registry, "all_collections"):
            cols = list(self._registry.all_collections())
        else:
            cols = []
            for agent in self._registry.list_agents():
                names = agent.collection_names() if hasattr(agent, "collection_names") else [agent.collection]
                cols.extend(names)
            cols = _dedupe_preserving_order(cols)
        if not cols:
            raise NotImplementedError(
                "scope=all-agents / scope=everything requires an AgentRegistry "
                "with at least one agent registered. The configured AgentRegistry "
                "is empty — add at least one entry to the `agents:` section of "
                "kairix.config.yaml: `agents: [{name: <agent>, write_path: <path>}]`."
            )
        return cols
