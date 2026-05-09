"""
Strategy pattern implementations for post-fusion boosting.

Wraps existing boost functions from kairix.core.search.rrf as BoostStrategy
protocol implementations. No logic duplication — delegates to the existing
functions.
"""

from __future__ import annotations

from kairix.core.protocols import GraphRepository
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    TemporalBoostConfig,
)
from kairix.core.search.intent import QueryIntent


class EntityBoost:
    """Boost results based on Neo4j entity mention in-degree.

    Gated to ENTITY intent — non-ENTITY queries pass through unchanged.
    Requires a GraphRepository for entity lookup. Documents matching entity
    vault paths receive a log-scaled boost proportional to in-degree.
    """

    def __init__(
        self,
        graph: GraphRepository,
        config: EntityBoostConfig | None = None,
    ) -> None:
        self._graph = graph
        self._config = config

    def boost(self, results: list, query: str, context: dict) -> list:
        if context.get("intent") != QueryIntent.ENTITY:
            return results
        from kairix.core.search.rrf import entity_boost_neo4j

        return entity_boost_neo4j(results, self._graph, config=self._config)


class ProceduralBoost:
    """Boost procedural content (how-to guides, runbooks) by path pattern.

    Gated to PROCEDURAL intent — non-PROCEDURAL queries pass through unchanged.
    Multiplies boosted_score by config.factor for documents whose path matches
    procedural patterns.
    """

    def __init__(self, config: ProceduralBoostConfig | None = None) -> None:
        self._config = config

    def boost(self, results: list, query: str, context: dict) -> list:
        if context.get("intent") != QueryIntent.PROCEDURAL:
            return results
        from kairix.core.search.rrf import procedural_boost

        return procedural_boost(results, config=self._config)


class TemporalDateBoost:
    """Boost documents whose path contains a date matching the query.

    Gated to TEMPORAL intent — non-TEMPORAL queries pass through unchanged.
    Boosts documents with explicit date strings or recent dates for relative
    temporal terms.
    """

    def __init__(self, config: TemporalBoostConfig | None = None) -> None:
        self._config = config

    def boost(self, results: list, query: str, context: dict) -> list:
        if context.get("intent") != QueryIntent.TEMPORAL:
            return results
        from kairix.core.search.rrf import temporal_date_boost

        return temporal_date_boost(results, query, config=self._config)


class ChunkDateBoost:
    """Boost documents by chunk_date metadata proximity to query date.

    Gated to TEMPORAL intent — non-TEMPORAL queries pass through unchanged.
    Uses Gaussian decay based on the distance between chunk_date and the
    query date. Requires chunk_date to be populated at index time.
    """

    def __init__(self, config: TemporalBoostConfig | None = None) -> None:
        self._config = config

    def boost(self, results: list, query: str, context: dict) -> list:
        if context.get("intent") != QueryIntent.TEMPORAL:
            return results
        from kairix.core.search.rrf import chunk_date_boost

        query_date = context.get("query_date")
        return chunk_date_boost(results, query_date, config=self._config)
