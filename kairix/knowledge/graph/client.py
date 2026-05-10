"""
kairix.knowledge.graph.client — Neo4j connection and upsert helpers.

Configured via env vars:
  KAIRIX_NEO4J_URI      (default: bolt://localhost:7687)
  KAIRIX_NEO4J_USER     (default: neo4j)
  KAIRIX_NEO4J_PASSWORD (required — no default)

Neo4j is required for ENTITY intent queries. Callers handling ENTITY
intent must check client.available before use; hybrid.py enforces this
by failing fast with a SearchResult.error when Neo4j is unavailable.

Upsert and write methods return bool/None rather than raising — failures
are logged as warnings and are expected to be retried on the next crawl.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kairix.knowledge.graph.models import (
    ConceptNode,
    FrameworkNode,
    GraphEdge,
    OrganisationNode,
    OutcomeNode,
    PersonNode,
    PublicationNode,
    TechnologyNode,
)
from kairix.secrets import load_secrets as _load_secrets

# Load vault-agent sidecar secrets before env-var reads.
# No-op when /run/secrets/kairix.env is absent (local dev, CI).
_load_secrets()

# Suppress verbose Neo4j driver notifications (harmless on empty graphs)
logging.getLogger("neo4j").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


def _get_neo4j_defaults() -> tuple[str, str, str]:
    """Resolve Neo4j credentials lazily via get_credentials("graph")."""
    try:
        from kairix.credentials import GraphCredentials, get_credentials

        creds = get_credentials("graph")
        if isinstance(creds, GraphCredentials):
            return creds.uri, creds.user, creds.password
    except Exception:  # noqa: S110 — fallback to env vars below
        pass
    # Fallback to env vars for backwards compatibility
    return (
        os.environ.get("KAIRIX_NEO4J_URI", "bolt://localhost:7687"),
        os.environ.get("KAIRIX_NEO4J_USER", "neo4j"),
        os.environ.get("KAIRIX_NEO4J_PASSWORD", ""),
    )


# Constraints ensure idempotent upserts via MERGE on id property
_CONSTRAINT_QUERIES = [
    "CREATE CONSTRAINT organisation_id IF NOT EXISTS FOR (n:Organisation) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT person_id IF NOT EXISTS FOR (n:Person) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT outcome_id IF NOT EXISTS FOR (n:Outcome) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT document_id IF NOT EXISTS FOR (n:Document) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT concept_id IF NOT EXISTS FOR (n:Concept) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT framework_id IF NOT EXISTS FOR (n:Framework) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT technology_id IF NOT EXISTS FOR (n:Technology) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT publication_id IF NOT EXISTS FOR (n:Publication) REQUIRE n.id IS UNIQUE",
]


def _try_import_neo4j() -> Any:
    try:
        from neo4j import GraphDatabase

        return GraphDatabase
    except ImportError:
        return None


def _redact_uri(uri: str) -> str:
    """Strip any embedded ``user:pass@`` credentials from a URI before logging.

    Defensive against misuse of ``KAIRIX_NEO4J_URI`` — the documented
    convention separates credentials from the URI (auth tuple), but
    operators occasionally embed them inline. This redaction guarantees
    we never log plaintext credentials even if someone does.
    """
    from urllib.parse import urlparse, urlunparse

    try:
        parts = urlparse(uri)
    except ValueError:
        return uri  # malformed URI — return as-is rather than mangle further
    if not parts.hostname:
        return uri
    netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunparse(parts._replace(netloc=netloc))


class Neo4jClient:
    """
    Thin wrapper over the neo4j Python driver.

    Instantiate with Neo4jClient() — reads env vars automatically.
    Calling code should check client.available before using graph methods.
    """

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        if uri is None or user is None or password is None:
            _uri, _user, _password = _get_neo4j_defaults()
            uri = uri or _uri
            user = user or _user
            password = password or _password
        self._uri = uri
        self._user = user
        self._password = password
        self._driver: Any = None
        self.available = False
        self._connect()

    def _connect(self) -> None:
        driver_cls = _try_import_neo4j()
        if driver_cls is None:
            logger.warning("Neo4jClient: neo4j driver not installed — graph layer unavailable")
            return
        if not self._password:
            logger.warning("Neo4jClient: KAIRIX_NEO4J_PASSWORD not set — graph layer unavailable")
            return
        try:
            self._driver = driver_cls.driver(self._uri, auth=(self._user, self._password))
            self._driver.verify_connectivity()
            self.available = True
            logger.info("Neo4jClient: connected to %s", _redact_uri(self._uri))
            self._init_constraints()
        except Exception as e:  # broad: neo4j raises diverse types on connect fail
            logger.warning("Neo4jClient: connection failed — %s", e)
            self._driver = None

    def _init_constraints(self) -> None:
        if not self._driver:
            return
        with self._driver.session() as session:
            for q in _CONSTRAINT_QUERIES:
                try:
                    session.run(q)
                except (RuntimeError, OSError) as e:
                    logger.warning("Neo4jClient: constraint init failed — %s", e)

    def close(self) -> None:
        if self._driver:
            self._driver.close()

    # -------------------------------------------------------------------------
    # Upsert methods — idempotent MERGE on node id
    # -------------------------------------------------------------------------

    def upsert_node(self, label: str, node_id: str, props: dict[str, Any]) -> bool:
        """Generic node upsert — MERGE on id, SET properties."""
        if not self._driver:
            return False
        try:
            with self._driver.session() as session:
                session.run(
                    f"MERGE (n:{label} {{id: $id}}) SET n += $props",
                    id=node_id,
                    props=props,
                )
            return True
        except Exception as e:
            logger.warning("upsert_%s(%s): %s", label.lower(), node_id, e)
            return False

    def upsert_organisation(self, node: OrganisationNode) -> bool:
        return self.upsert_node("Organisation", node.id, node.to_neo4j_props())

    def upsert_person(self, node: PersonNode) -> bool:
        return self.upsert_node("Person", node.id, node.to_neo4j_props())

    def upsert_outcome(self, node: OutcomeNode) -> bool:
        return self.upsert_node("Outcome", node.id, node.to_neo4j_props())

    def upsert_concept(self, node: ConceptNode) -> bool:
        return self.upsert_node("Concept", node.id, node.to_neo4j_props())

    def upsert_framework(self, node: FrameworkNode) -> bool:
        return self.upsert_node("Framework", node.id, node.to_neo4j_props())

    def upsert_technology(self, node: TechnologyNode) -> bool:
        return self.upsert_node("Technology", node.id, node.to_neo4j_props())

    def upsert_publication(self, node: PublicationNode) -> bool:
        return self.upsert_node("Publication", node.id, node.to_neo4j_props())

    def upsert_edge(self, edge: GraphEdge) -> bool:
        if not self._driver:
            return False
        if edge.from_label == "Document":
            # Document nodes are not pre-created; MERGE creates them on first MENTIONS edge.
            # Using MATCH here was a silent no-op — Document nodes never exist at time of call.
            cypher = (
                f"MERGE (a:Document {{id: $from_id}}) "
                f"WITH a "
                f"MATCH (b:{edge.to_label} {{id: $to_id}}) "
                f"MERGE (a)-[r:{edge.kind.value}]->(b) "
                "SET r += $props"
            )
        else:
            # Known entity labels are guaranteed to exist via upsert_entity — MATCH is safe.
            cypher = (
                f"MATCH (a:{edge.from_label} {{id: $from_id}}) "
                f"MATCH (b:{edge.to_label} {{id: $to_id}}) "
                f"MERGE (a)-[r:{edge.kind.value}]->(b) "
                "SET r += $props"
            )
        try:
            with self._driver.session() as session:
                result = session.run(cypher, from_id=edge.from_id, to_id=edge.to_id, props=edge.props)
                summary = result.consume()
                if summary.counters.relationships_created == 0 and summary.counters.properties_set == 0:
                    logger.warning(
                        "upsert_edge(%s→%s %s): no-op — target %s:%s may not exist",
                        edge.from_id,
                        edge.to_id,
                        edge.kind,
                        edge.to_label,
                        edge.to_id,
                    )
            return True
        except Exception as e:
            logger.warning("upsert_edge(%s→%s %s): %s", edge.from_id, edge.to_id, edge.kind, e)
            return False

    # -------------------------------------------------------------------------
    # Query methods
    # -------------------------------------------------------------------------

    def get_organisation(self, entity_id: str) -> dict[str, Any] | None:
        """Return org node properties by id, or None if not found."""
        if not self._driver:
            return None
        try:
            with self._driver.session() as session:
                result = session.run(
                    "MATCH (n:Organisation {id: $id}) RETURN n",
                    id=entity_id,
                )
                record = result.single()
                return dict(record["n"]) if record else None
        except Exception as e:
            logger.warning("get_organisation(%s): %s", entity_id, e)
            return None

    def get_person(self, entity_id: str) -> dict[str, Any] | None:
        """Return person node properties by id, or None if not found."""
        if not self._driver:
            return None
        try:
            with self._driver.session() as session:
                result = session.run(
                    "MATCH (n:Person {id: $id}) RETURN n",
                    id=entity_id,
                )
                record = result.single()
                return dict(record["n"]) if record else None
        except Exception as e:
            logger.warning("get_person(%s): %s", entity_id, e)
            return None

    def related_entities(self, entity_id: str, max_hops: int = 2) -> list[dict[str, Any]]:
        """
        Return entities connected to entity_id within max_hops.

        Used by CONTEXTUAL_PREP cross-entity expansion.
        Returns [] if Neo4j unavailable or entity not found.
        """
        if not self._driver:
            return []
        # Clamp max_hops to prevent unbounded graph traversal (DoS mitigation).
        # Strict int() cast prevents type-confusion injection.
        # Note: Cypher range literals (e.g. [*1..N]) do not support $param
        # binding, so f-string interpolation is required here — the int cast
        # and clamp guarantee a safe literal value.
        max_hops = min(max(1, int(max_hops)), 5)
        try:
            with self._driver.session() as session:
                result = session.run(
                    f"""
                    MATCH (start {{id: $id}})-[*1..{max_hops}]-(related)
                    WHERE related.id <> $id
                    RETURN DISTINCT related.id AS id, labels(related)[0] AS label,
                           related.name AS name, related.industry AS industry,
                           related.interests AS interests
                    LIMIT 20
                    """,
                    id=entity_id,
                )
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning("related_entities(%s): %s", entity_id, e)
            return []

    def find_by_name(self, name: str) -> list[dict[str, Any]]:
        """
        Search for nodes by name or alias (case-insensitive).

        Used by entity extraction in CONTEXTUAL_PREP to resolve query mentions.
        """
        if not self._driver:
            return []
        try:
            name_lower = name.lower()
            with self._driver.session() as session:
                result = session.run(
                    """
                    MATCH (n)
                    WHERE toLower(n.name) CONTAINS $name
                       OR any(alias IN n.aliases WHERE toLower(alias) CONTAINS $name)
                    RETURN n.id AS id, labels(n)[0] AS label, n.name AS name
                    LIMIT 10
                    """,
                    name=name_lower,
                )
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning("find_by_name(%s): %s", name, e)
            return []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """
        Execute an arbitrary read Cypher query.

        Intended for MCP kairix.graph_query tool (scoped reads only).
        Returns [] on any error.
        """
        if not self._driver:
            return []
        try:
            # Enforce read-only to prevent accidental graph mutation via arbitrary queries
            with self._driver.session(default_access_mode="READ") as session:
                result = session.run(query, **(params or {}))
                return [dict(r) for r in result]
        except Exception as e:
            logger.warning("cypher query failed: %s", e)
            return []

    def rotate_password(self, new_password: str) -> bool:
        """Change the Neo4j password for the current user.

        Connects with the existing password and executes ALTER CURRENT USER
        SET PASSWORD. Returns True on success, False on failure.
        """
        if not self._driver:
            logger.error("rotate_password: no active connection")
            return False
        try:
            with self._driver.session(database="system") as session:
                session.run(
                    "ALTER CURRENT USER SET PASSWORD FROM $old TO $new",
                    old=self._password,
                    new=new_password,
                )
            logger.info("Neo4j password rotated successfully")
            self._password = new_password
            return True
        except Exception as e:
            logger.error("rotate_password failed: %s", e)
            return False


# Module-level singleton — lazy-initialised on first call
_client: Neo4jClient | None = None


def get_client() -> Neo4jClient:
    """Return the module-level Neo4j client singleton."""
    global _client
    if _client is None:
        _client = Neo4jClient()
    return _client
