"""Tests for Neo4j upsert_edge — Document MENTIONS edge handling.

Verifies the fix for issue #40: Document nodes must be created via MERGE
(not MATCH) since they don't exist before the first MENTIONS edge.
These tests mock the Neo4j driver to verify Cypher query construction
without requiring a live database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kairix.knowledge.graph.models import EdgeKind, GraphEdge

pytestmark = pytest.mark.unit


@pytest.fixture()
def mock_neo4j_client() -> MagicMock:
    """Create a Neo4jClient with a mocked driver via the ``driver_cls=``
    constructor seam (F1-clean — no @patch on kairix internals)."""
    mock_driver_cls = MagicMock()
    mock_driver = MagicMock()
    mock_driver_cls.driver.return_value = mock_driver
    mock_driver.verify_connectivity.return_value = None

    from kairix.knowledge.graph.client import Neo4jClient

    client = Neo4jClient(
        uri="bolt://test:7687",
        user="test",
        password="test",  # pragma: allowlist secret
        driver_cls=mock_driver_cls,
    )
    assert client.available
    return client


class TestDocumentMentionsEdge:
    """upsert_edge creates Document nodes via MERGE for MENTIONS edges."""

    @pytest.mark.unit
    def test_document_source_uses_merge_not_match(self, mock_neo4j_client: MagicMock) -> None:
        """Document from_label uses MERGE to create node if it doesn't exist."""
        edge = GraphEdge(
            from_id="meeting-notes",
            from_label="Document",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.MENTIONS,
            props={"source_path": "projects/meeting-notes.md"},
        )
        mock_neo4j_client.upsert_edge(edge)

        session = mock_neo4j_client._driver.session.return_value.__enter__.return_value
        cypher = session.run.call_args[0][0]
        assert "MERGE (a:Document" in cypher
        assert "MATCH (b:Person" in cypher

    @pytest.mark.unit
    def test_non_document_source_uses_match(self, mock_neo4j_client: MagicMock) -> None:
        """Non-Document from_label uses MATCH (nodes must pre-exist)."""
        edge = GraphEdge(
            from_id="acme-corp",
            from_label="Organisation",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.WORKS_AT,
            props={},
        )
        mock_neo4j_client.upsert_edge(edge)

        session = mock_neo4j_client._driver.session.return_value.__enter__.return_value
        cypher = session.run.call_args[0][0]
        assert "MATCH (a:Organisation" in cypher
        assert "MATCH (b:Person" in cypher
        assert "MERGE" not in cypher.split("MATCH")[0]  # No MERGE before first MATCH

    @pytest.mark.unit
    def test_upsert_edge_returns_false_on_driver_error(self, mock_neo4j_client: MagicMock) -> None:
        """upsert_edge returns False (not raises) when Neo4j errors."""
        session = mock_neo4j_client._driver.session.return_value.__enter__.return_value
        session.run.side_effect = RuntimeError("Neo4j connection lost")

        edge = GraphEdge(
            from_id="doc-1",
            from_label="Document",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.MENTIONS,
            props={},
        )
        result = mock_neo4j_client.upsert_edge(edge)
        assert result is False

    @pytest.mark.unit
    def test_upsert_edge_returns_true_on_success(self, mock_neo4j_client: MagicMock) -> None:
        """upsert_edge returns True when edge creation succeeds."""
        edge = GraphEdge(
            from_id="doc-1",
            from_label="Document",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.MENTIONS,
            props={},
        )
        result = mock_neo4j_client.upsert_edge(edge)
        assert result is True

    @pytest.mark.unit
    def test_upsert_edge_returns_false_when_driver_none(self) -> None:
        """upsert_edge returns False when no Neo4j driver is available.

        F1-clean: pass driver_cls=None directly through the constructor seam
        instead of @patch'ing _try_import_neo4j to return None.
        """
        from kairix.knowledge.graph.client import Neo4jClient

        client = Neo4jClient(
            uri="bolt://test:7687",
            user="test",
            password="test",  # pragma: allowlist secret
            driver_cls=None,
        )
        assert not client.available

        edge = GraphEdge(
            from_id="doc-1",
            from_label="Document",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.MENTIONS,
            props={},
        )
        result = client.upsert_edge(edge)
        assert result is False

    @pytest.mark.unit
    def test_mentions_edge_sets_properties(self, mock_neo4j_client: MagicMock) -> None:
        """MENTIONS edge passes props to SET r += $props."""
        edge = GraphEdge(
            from_id="doc-1",
            from_label="Document",
            to_id="alice",
            to_label="Person",
            kind=EdgeKind.MENTIONS,
            props={"source_path": "projects/doc-1.md", "weight": 1.0},
        )
        mock_neo4j_client.upsert_edge(edge)

        session = mock_neo4j_client._driver.session.return_value.__enter__.return_value
        call_kwargs = session.run.call_args[1]
        assert call_kwargs["props"] == {
            "source_path": "projects/doc-1.md",
            "weight": 1.0,
        }
