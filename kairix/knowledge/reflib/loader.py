"""
kairix.knowledge.reflib.loader — Load entity stubs from JSON into Neo4j.

Reads nodes.json and edges.json produced by the reference library entity
extraction pipeline, then upserts them into Neo4j using MERGE semantics
(idempotent — safe to call multiple times).

Usage:
    from kairix.knowledge.reflib.loader import load_entity_stubs
    report = load_entity_stubs(nodes_path, edges_path, neo4j_client)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.knowledge.graph.models import (
    ConceptNode,
    EdgeKind,
    FrameworkNode,
    GraphEdge,
    NodeLabel,
    OrganisationNode,
    OutcomeNode,
    PersonNode,
    PublicationNode,
    TechnologyNode,
)

logger = logging.getLogger(__name__)


@dataclass
class LoadReport:
    """Summary of a stub-loading run."""

    nodes_loaded: int = 0
    edges_loaded: int = 0
    nodes_skipped: int = 0
    edges_skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"Loaded {self.nodes_loaded} nodes, {self.edges_loaded} edges"


# ---------------------------------------------------------------------------
# Label → (node dataclass, upsert method name) dispatch table
# ---------------------------------------------------------------------------

_LABEL_DISPATCH: dict[str, tuple[type, str]] = {
    "Organisation": (OrganisationNode, "upsert_organisation"),
    "Person": (PersonNode, "upsert_person"),
    "Outcome": (OutcomeNode, "upsert_outcome"),
}

# Labels that don't have a dedicated upsert method on Neo4jClient.
# We build the dataclass for validation, then use a generic MERGE.
_GENERIC_LABELS: dict[str, type] = {
    "Concept": ConceptNode,
    "Framework": FrameworkNode,
    "Technology": TechnologyNode,
    "Publication": PublicationNode,
}


def _upsert_generic_node(neo4j_client: Any, label: str, node: Any) -> bool:
    """MERGE a node type that has no dedicated upsert method on the client."""
    result: bool = neo4j_client.upsert_node(label, node.id, node.to_neo4j_props())
    return result


def build_node(label: str, data: dict[str, Any]) -> Any:
    """Instantiate the correct node dataclass from a JSON dict.

    Returns (label, node_instance) or raises ValueError.
    """
    # Validate label
    try:
        NodeLabel(label)
    except ValueError as exc:
        raise ValueError(f"Unknown node label: {label!r}") from exc

    # Filter data to only keys accepted by the dataclass
    if label in _LABEL_DISPATCH:
        cls = _LABEL_DISPATCH[label][0]
    elif label in _GENERIC_LABELS:
        cls = _GENERIC_LABELS[label]
    elif label == "Document":
        # Document nodes are created implicitly by edge upserts — skip
        raise ValueError("Document nodes are created implicitly via edges; skip")
    else:
        raise ValueError(f"No handler for label: {label!r}")

    # Build kwargs from dataclass fields, ignoring unknown keys
    import dataclasses

    valid_fields = {f.name for f in dataclasses.fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in valid_fields and k != "label"}
    return cls(**kwargs)


def _build_edge(data: dict[str, Any]) -> GraphEdge:
    """Instantiate a GraphEdge from a JSON dict."""
    kind_str = data.get("kind", data.get("type", ""))
    try:
        kind = EdgeKind(kind_str)
    except ValueError as exc:
        raise ValueError(f"Unknown edge kind: {kind_str!r}") from exc

    return GraphEdge(
        from_id=data["from_id"],
        from_label=data["from_label"],
        to_id=data["to_id"],
        to_label=data["to_label"],
        kind=kind,
        props=data.get("props", {}),
    )


# ---------------------------------------------------------------------------
# Extracted helpers
# ---------------------------------------------------------------------------


def validate_and_build_node(
    entry: dict[str, Any],
    i: int,
    report: LoadReport,
) -> tuple[str, Any] | None:
    """Call build_node, catch errors, return (label, node) or None."""
    label = entry.get("label", "")
    node_id = entry.get("id", f"<index {i}>")
    try:
        node = build_node(label, entry)
    except ValueError as e:
        report.nodes_skipped += 1
        report.errors.append(f"Node {node_id}: {e}")
        return None
    return (label, node)


def validate_and_build_edge(
    entry: dict[str, Any],
    _i: int,
    report: LoadReport,
) -> GraphEdge | None:
    """Call _build_edge, catch errors, return edge or None."""
    edge_desc = f"{entry.get('from_id', '?')}→{entry.get('to_id', '?')}"
    try:
        return _build_edge(entry)
    except (ValueError, KeyError) as e:
        report.edges_skipped += 1
        report.errors.append(f"Edge {edge_desc}: {e}")
        return None


def load_nodes(
    nodes_path: Path,
    neo4j_client: Any,
    dry_run: bool,
    report: LoadReport,
) -> None:
    """Read nodes.json, validate and upsert each node."""
    if not nodes_path.exists():
        logger.warning("Nodes file not found: %s", nodes_path)
        report.errors.append(f"Nodes file not found: {nodes_path}")
        return

    try:
        raw_nodes = json.loads(nodes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        report.errors.append(f"Failed to read {nodes_path}: {e}")
        return

    for i, entry in enumerate(raw_nodes):
        result = validate_and_build_node(entry, i, report)
        if result is None:
            continue
        label, node = result

        if dry_run:
            report.nodes_loaded += 1
            continue

        if label in _LABEL_DISPATCH:
            method_name = _LABEL_DISPATCH[label][1]
            ok = getattr(neo4j_client, method_name)(node)
        elif label in _GENERIC_LABELS:
            ok = _upsert_generic_node(neo4j_client, label, node)
        else:
            ok = False

        if ok:
            report.nodes_loaded += 1
        else:
            report.nodes_skipped += 1
            node_id = entry.get("id", f"<index {i}>")
            report.errors.append(f"Node {node_id}: upsert failed")


def load_edges(
    edges_path: Path,
    neo4j_client: Any,
    dry_run: bool,
    report: LoadReport,
) -> None:
    """Read edges.json, validate and upsert each edge."""
    if not edges_path.exists():
        logger.warning("Edges file not found: %s", edges_path)
        report.errors.append(f"Edges file not found: {edges_path}")
        return

    try:
        raw_edges = json.loads(edges_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        report.errors.append(f"Failed to read {edges_path}: {e}")
        return

    for i, entry in enumerate(raw_edges):
        edge = validate_and_build_edge(entry, i, report)
        if edge is None:
            continue

        if dry_run:
            report.edges_loaded += 1
            continue

        edge_desc = f"{entry.get('from_id', '?')}→{entry.get('to_id', '?')}"
        if neo4j_client.upsert_edge(edge):
            report.edges_loaded += 1
        else:
            report.edges_skipped += 1
            report.errors.append(f"Edge {edge_desc}: upsert failed")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_entity_stubs(
    nodes_path: Path,
    edges_path: Path,
    neo4j_client: Any,
    dry_run: bool = False,
) -> LoadReport:
    """Load entity stubs from JSON files into Neo4j.

    Args:
        nodes_path: Path to nodes.json (list of node dicts with 'label' field).
        edges_path: Path to edges.json (list of edge dicts).
        neo4j_client: Neo4jClient instance from kairix.knowledge.graph.client.
        dry_run: If True, parse and validate but do not write to Neo4j.

    Returns:
        LoadReport with counts and any errors encountered.
    """
    report = LoadReport()

    if not dry_run and (neo4j_client is None or not neo4j_client.available):
        logger.warning("Neo4j unavailable — returning empty load report")
        report.errors.append("Neo4j unavailable")
        return report

    load_nodes(nodes_path, neo4j_client, dry_run, report)
    load_edges(edges_path, neo4j_client, dry_run, report)

    return report
