"""AgentRegistry — declarative agent → collection mapping.

The multi-agent memory architecture (vault doc agent-memory-architecture-
recommendation-2026-04-16) needs kairix to know which agents exist for
two operations the historical code couldn't do:

  - Resolve scope=all-agents / scope=everything to a concrete list of
    collection names (DefaultCollectionResolver consults the registry).
  - Validate that an embed-pipeline write under a path tagged for one
    agent is being performed by that agent (write isolation).

YAML schema (sits alongside collections in kairix.config.yaml):

    agents:
      - name: alpha
        collection: alpha-memory          # optional, derived via agent_pattern
        write_path: 04-Agent-Knowledge/alpha/memory
        read_only: false                  # optional; true skips write validation
      - name: beta
        ...

When ``collection`` is omitted the registry derives it from the
collections config's agent_pattern (default ``{agent}-memory``). When
the YAML has no ``agents:`` section the registry is empty — callers
get explicit NotImplementedError for ALL_AGENTS / EVERYTHING scope so
the misconfiguration is loud, not silent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class AgentDef:
    """One row of the agent registry."""

    name: str
    collection: str
    write_path: str = ""
    read_only: bool = False


class ConfigDrivenAgentRegistry:
    """In-memory registry built from a list of AgentDef.

    Constructed at the boundary (factory.py) from the parsed YAML.
    Tests pass a list directly via FakeAgentRegistry in tests/fakes.py.
    """

    def __init__(self, agents: list[AgentDef] | None = None) -> None:
        self._by_name: dict[str, AgentDef] = {a.name: a for a in (agents or [])}

    def list_agents(self) -> list[AgentDef]:
        return list(self._by_name.values())

    def collection_for(self, name: str) -> str:
        agent = self._by_name.get(name)
        if agent is None:
            raise KeyError(f"unknown agent {name!r}; registered: {sorted(self._by_name)}")
        return agent.collection

    def validate_write(self, agent_name: str, path: str) -> bool:
        """Return True iff ``path`` is under ``agent_name``'s declared write_path.

        An agent with no write_path declared (or read_only=True) returns False
        for any path — strict by default. Operators who want a permissive
        deployment set read_only=False and a wildcard write_path.
        """
        agent = self._by_name.get(agent_name)
        if agent is None or agent.read_only:
            return False
        if not agent.write_path:
            return False
        return path == agent.write_path or path.startswith(agent.write_path.rstrip("/") + "/")


def build_agent_owner_resolver(
    registry: ConfigDrivenAgentRegistry,
) -> Callable[[str, str], str | None]:
    """Build the (collection, rel_path) → agent_name resolver for the embed scanner.

    The resolver matches each scanned document against every agent's
    ``write_path``. The longest-prefix match wins (so ``shared/foo`` doesn't
    accidentally match an agent whose write_path is ``shared``). Documents
    not under any agent's write_path return None and land in the database
    with ``agent_owner=NULL`` (treated as shared).

    Used by ``kairix/core/embed/cli.py`` to wire ``DocumentScanner`` with
    per-document agent provenance (#114).
    """
    # Stable list snapshot at build time so tests / production both bind once.
    agents = [a for a in registry.list_agents() if a.write_path]
    # Sort by descending write_path length so longest match wins via "first hit".
    agents.sort(key=lambda a: len(a.write_path), reverse=True)

    def _resolve(_collection: str, rel_path: str) -> str | None:
        for agent in agents:
            wp = agent.write_path.rstrip("/")
            if rel_path == wp or rel_path.startswith(wp + "/"):
                return agent.name
        return None

    return _resolve


def parse_agent_registry(data: dict, *, default_pattern: str = "{agent}-memory") -> ConfigDrivenAgentRegistry:
    """Parse the agents: section out of a top-level YAML dict.

    Returns an empty registry when the section is missing — callers get
    explicit NotImplementedError on ALL_AGENTS / EVERYTHING scope rather
    than silent fallback to "search nothing".
    """
    agents_raw = data.get("agents")
    if not agents_raw:
        return ConfigDrivenAgentRegistry(agents=[])

    agents: list[AgentDef] = []
    for item in agents_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        collection = item.get("collection") or default_pattern.format(agent=name)
        agents.append(
            AgentDef(
                name=str(name),
                collection=str(collection),
                write_path=str(item.get("write_path", "")),
                read_only=bool(item.get("read_only", False)),
            )
        )
    return ConfigDrivenAgentRegistry(agents=agents)
