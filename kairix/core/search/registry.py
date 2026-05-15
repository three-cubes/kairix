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

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_AGENT_WORKSPACE_TEMPLATE = "/data/workspaces/{name}"

# Collection names that are reserved at the structural layer because the
# embed harness auto-injects a collection with that exact name (see
# ``kairix/core/embed/cli.py``). An agent that also claims one of these
# names via the legacy ``collection:`` field would shadow the auto-injected
# collection in search routing, producing incorrect results. We therefore
# refuse to honour the override at parse time and substitute the agent's
# synthetic ``{name}-{i}`` naming, with a logged warning.
#
# This is *not* a policy reserve (which would belong in the operator's
# yaml as ``in_default: false``) — it is a name-collision guard against
# collections the runtime creates outside the YAML config surface.
RESERVED_AGENT_COLLECTION_NAMES: frozenset[str] = frozenset({"reference-library"})


@dataclass
class AgentDef:
    """One row of the agent registry.

    The agent's *read* surface is ``paths`` — one or more locations the
    agent reads from. Each path produces a synthetic collection at
    registry-load time so the search resolver can join across them.

    The agent's *write* surface is ``write_path`` — the single canonical
    write zone. Typically the first entry in ``paths``, but the schema
    allows them to differ (e.g. read-only access to a shared cross-agent
    knowledge area).

    When ``paths`` is omitted the agent gets ``["/data/workspaces/{name}"]``
    by default — kairix no longer bakes the historical ``04-Agent-Knowledge``
    layout into shipped code.
    """

    name: str
    paths: list[str] = field(default_factory=list)
    write_path: str = ""
    read_only: bool = False
    # Optional legacy override: when an old YAML had ``collection: alpha-memory``
    # we honour that name for the agent's first synthetic collection so existing
    # search calls / saved benchmarks keep resolving. New deployments leave
    # this empty and the synthetic ``{name}-{i}`` naming is used.
    legacy_collection_name: str = ""

    @property
    def effective_paths(self) -> list[str]:
        """Paths to read from.

        Resolution order, first non-empty wins:
          1. ``paths`` if explicitly declared (multi-path schema).
          2. ``[write_path]`` for legacy single-path agents — the write zone
             is also the read scope when no separate ``paths`` was given.
          3. ``["/data/workspaces/{name}"]`` — the out-of-the-box default.
        """
        if self.paths:
            return list(self.paths)
        if self.write_path:
            return [self.write_path]
        return [DEFAULT_AGENT_WORKSPACE_TEMPLATE.format(name=self.name)]

    def collection_names(self) -> list[str]:
        """Synthetic collection names for this agent's effective paths.

        First name honours ``legacy_collection_name`` if the YAML supplied
        the old ``collection:`` field. Subsequent collections use the
        ``{name}-{i}`` convention. Consumers should call this method, not
        index into ``paths`` — the latter pattern is the inappropriate-
        intimacy code smell.
        """
        names = [f"{self.name}-{i}" for i in range(len(self.effective_paths))]
        if self.legacy_collection_name and names:
            names[0] = self.legacy_collection_name
        return names

    def resolved_paths(self, document_root: Path) -> list[Path]:
        """Each effective path resolved to absolute Path.

        Absolute paths used as-is. Relative paths joined with document_root.
        Used by the embed scanner to know which directories to scan for
        this agent's documents.
        """
        result: list[Path] = []
        for raw in self.effective_paths:
            p = Path(raw)
            result.append(p if p.is_absolute() else document_root / raw)
        return result

    def owns_path(self, rel_path: str) -> bool:
        """True iff ``rel_path`` is under any of this agent's declared paths.

        Used by the embed scanner to tag documents with ``agent_owner`` and
        by the registry's ``validate_write``. An agent with ``read_only=True``
        owns nothing. An agent with neither ``paths`` nor ``write_path``
        falls back to the default workspace template via ``effective_paths``,
        so ``owns_path`` still has a meaningful answer.

        For overlapping cross-agent paths (e.g. ``04-Agent-Knowledge/shared``
        declared by both shape and builder) this returns True for both
        agents — disambiguation via longest-prefix-match happens in the
        ``build_agent_owner_resolver`` factory.
        """
        if self.read_only:
            return False
        for raw in self.effective_paths:
            wp = raw.rstrip("/")
            if not wp:
                continue
            if rel_path == wp or rel_path.startswith(wp + "/"):
                return True
        return False

    def claims_write(self, rel_path: str) -> bool:
        """True iff ``rel_path`` is under this agent's canonical ``write_path``.

        Stricter than :meth:`owns_path` — only the single declared write
        zone counts. Used by the registry's ``validate_write`` to enforce
        that a write operation by ``agent_name`` lands inside that agent's
        designated write area.
        """
        if self.read_only or not self.write_path:
            return False
        wp = self.write_path.rstrip("/")
        return rel_path == wp or rel_path.startswith(wp + "/")

    @property
    def collection(self) -> str:
        """Single-collection accessor preserved for legacy callers.

        Returns the first synthetic collection name. New code should use
        :meth:`collection_names` to handle multi-path agents correctly.
        """
        names = self.collection_names()
        return names[0] if names else ""


class ConfigDrivenAgentRegistry:
    """In-memory registry built from a list of AgentDef.

    Constructed at the boundary (factory.py) from the parsed YAML.
    Tests pass a list directly via FakeAgentRegistry in tests/fakes.py.
    """

    def __init__(self, agents: list[AgentDef] | None = None) -> None:
        self._by_name: dict[str, AgentDef] = {a.name: a for a in (agents or [])}

    def list_agents(self) -> list[AgentDef]:
        return list(self._by_name.values())

    def get(self, name: str) -> AgentDef:
        """Look up an agent by name, raising KeyError if unknown."""
        agent = self._by_name.get(name)
        if agent is None:
            raise KeyError(f"unknown agent {name!r}; registered: {sorted(self._by_name)}")
        return agent

    def collection_for(self, name: str) -> str:
        """Legacy single-collection accessor — first synthetic name."""
        return self.get(name).collection

    def collections_for(self, name: str) -> list[str]:
        """All synthetic collection names for a given agent."""
        return self.get(name).collection_names()

    def all_collections(self) -> list[str]:
        """Every agent's collection names, deduped, in registration order.

        Used by ``DefaultCollectionResolver`` for ``scope=all-agents`` and
        ``scope=everything``. The dedupe matters when multiple agents share
        a path (e.g. ``04-Agent-Knowledge/shared``).
        """
        seen: set[str] = set()
        out: list[str] = []
        for agent in self._by_name.values():
            for col in agent.collection_names():
                if col not in seen:
                    seen.add(col)
                    out.append(col)
        return out

    def validate_write(self, agent_name: str, path: str) -> bool:
        """Return True iff ``path`` is under the agent's declared write_path.

        Delegates to :meth:`AgentDef.claims_write` — strict write-zone check,
        distinct from the broader :meth:`AgentDef.owns_path` (which considers
        the agent's full read surface).
        """
        agent = self._by_name.get(agent_name)
        if agent is None:
            return False
        return agent.claims_write(path)


def build_agent_owner_resolver(
    registry: ConfigDrivenAgentRegistry,
) -> Callable[[str, str], str | None]:
    """Build the (collection, rel_path) → agent_name resolver for the embed scanner.

    Walks every agent's *effective_paths* (read surface) and returns the
    name of the agent whose longest matching path wins. When two agents
    declare the same path (e.g. a shared cross-agent area) the one
    registered *first* in YAML wins by stable-sort tie-breaking — operators
    can flip this by re-ordering the ``agents:`` list.

    Documents not under any agent's path return ``None`` and land in the
    database with ``agent_owner=NULL`` (treated as shared / unowned).

    Used by ``kairix/core/embed/cli.py`` to wire ``DocumentScanner`` with
    per-document agent provenance (#114).
    """
    # Build a flat list of (path, agent_name) tuples covering every effective
    # path of every non-read-only agent, then sort by descending path length
    # so longest match wins via first-hit semantics.
    candidates: list[tuple[str, str]] = []
    for agent in registry.list_agents():
        if agent.read_only:
            continue
        for raw in agent.effective_paths:
            wp = raw.rstrip("/")
            if wp:
                candidates.append((wp, agent.name))
    # Stable sort: ties keep YAML-declaration order so operators have a
    # deterministic knob.
    candidates.sort(key=lambda c: -len(c[0]))

    def _resolve(_collection: str, rel_path: str) -> str | None:
        for wp, name in candidates:
            if rel_path == wp or rel_path.startswith(wp + "/"):
                return name
        return None

    return _resolve


def _parse_paths_list(name: str, paths_raw: object) -> list[str]:
    """Coerce the ``paths:`` YAML field into a list of non-empty strings."""
    if not isinstance(paths_raw, list):
        logger.warning("agent %r: 'paths' must be a list — ignoring %r", name, paths_raw)
        return []
    return [str(p) for p in paths_raw if p]


# Per-process dedup for the legacy `collection:` deprecation warning. Each
# (agent_name, candidate) emits once across the lifetime of the process —
# the benchmark and eval paths re-parse the agent registry per case, which
# previously produced N_agents x N_cases warning lines (~1400 for reflib;
# #275) and drowned out real signal in container stderr / journal output.
_LEGACY_COLLECTION_WARNED: set[tuple[str, str]] = set()


def _resolve_legacy_collection_name(
    name: str,
    item: dict,
    write_path: str,
    paths: list[str],
    default_pattern: str,
) -> str:
    """Decide ``AgentDef.legacy_collection_name`` for a parsed YAML entry.

    Rules:
      - If ``collection:`` is supplied and clashes with a reserved name,
        log a warning and drop the override.
      - Otherwise the legacy collection name flows through with a
        deprecation warning pointing at the multi-path schema. The
        deprecation warning is deduplicated per (agent, candidate) so
        repeated re-parsing (e.g. per benchmark case) doesn't drown out
        other signal.
      - Fully-default agents (no ``paths``, no ``collection``) keep the
        ``default_pattern``-derived label so existing benchmarks pinned to
        ``{agent}-memory`` continue to resolve.
    """
    if "collection" in item:
        candidate = str(item["collection"])
        if candidate in RESERVED_AGENT_COLLECTION_NAMES:
            logger.warning(
                "agent %r: legacy collection name %r clashes with an "
                "auto-injected collection — ignoring override; the agent "
                "will use synthetic collection naming instead.",
                name,
                candidate,
            )
            return ""
        warn_key = (name, candidate)
        if warn_key not in _LEGACY_COLLECTION_WARNED:
            _LEGACY_COLLECTION_WARNED.add(warn_key)
            logger.warning(
                "agent %r: 'collection: %s' is deprecated — prefer the multi-path "
                "schema 'paths: [%s]'. The legacy field still parses but will be "
                "removed in a future release. (#115)",
                name,
                candidate,
                write_path or "/data/workspaces/" + str(name),
            )
        return candidate
    if not paths:
        return default_pattern.format(agent=str(name))
    return ""


def _parse_one_agent(item: object, default_pattern: str) -> AgentDef | None:
    """Build a single ``AgentDef`` from one YAML mapping entry, or ``None``
    when the entry is malformed (non-dict, missing name).
    """
    if not isinstance(item, dict):
        return None
    name = item.get("name")
    if not name:
        return None

    paths = _parse_paths_list(str(name), item.get("paths") or [])
    write_path = str(item.get("write_path", ""))
    if not paths and write_path:
        paths = [write_path]

    legacy_name = _resolve_legacy_collection_name(str(name), item, write_path, paths, default_pattern)
    return AgentDef(
        name=str(name),
        paths=paths,
        write_path=write_path,
        read_only=bool(item.get("read_only", False)),
        legacy_collection_name=legacy_name,
    )


def parse_agent_registry(data: dict, *, default_pattern: str = "{agent}-memory") -> ConfigDrivenAgentRegistry:
    """Parse the ``agents:`` section out of a top-level YAML dict.

    Returns an empty registry when the section is missing — callers get
    explicit ``NotImplementedError`` on ALL_AGENTS / EVERYTHING scope
    rather than silent fallback to "search nothing".

    Schema (multi-path, current):

        agents:
          - name: alpha
            paths:
              - /data/workspaces/alpha
              - 04-Agent-Knowledge/alpha
            write_path: /data/workspaces/alpha
            read_only: false

    Schema (legacy, still parses with a deprecation warning):

        agents:
          - name: alpha
            collection: alpha-memory      # → legacy_collection_name
            write_path: 04-Agent-Knowledge/alpha

    Backwards-compat resolution order:
      1. ``paths:`` if present → ``AgentDef.paths``.
      2. ``write_path:`` only → ``paths = [write_path]``.
      3. neither → ``paths = []`` (default workspace via effective_paths).
    The ``collection:`` field, if present, becomes ``legacy_collection_name``
    so the agent's first synthetic collection keeps the operator-chosen name
    instead of switching to ``{name}-0``. The ``default_pattern`` argument is
    preserved for the same legacy reason: when the operator omits ``paths``
    and ``write_path`` but supplied ``collection:`` previously, that name is
    honoured.
    """
    agents_raw = data.get("agents")
    if not agents_raw:
        return ConfigDrivenAgentRegistry(agents=[])

    agents: list[AgentDef] = []
    for item in agents_raw:
        parsed = _parse_one_agent(item, default_pattern)
        if parsed is not None:
            agents.append(parsed)
    return ConfigDrivenAgentRegistry(agents=agents)
