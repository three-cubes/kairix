"""End-to-end integration tests for reference-library install + status.

Wires the reflib loader pipeline against a writable Neo4j fake and a
``tmp_path``-rooted reflib directory:

  - ``load_entity_stubs`` (orchestrator)
  - ``load_nodes`` + ``load_edges`` (the validate-and-upsert helpers)
  - ``build_node`` + ``_build_edge`` (dataclass construction)

The Neo4j boundary is a recording fake (``_WritableFakeNeo4j``) — every
``upsert_organisation`` / ``upsert_person`` / ``upsert_node`` /
``upsert_edge`` call is recorded so the test can assert what landed.
``tmp_path`` holds the nodes.json/edges.json fixture.

What's covered here that unit + BDD don't catch:
  - The dispatch table (``_LABEL_DISPATCH`` vs ``_GENERIC_LABELS``)
    routes Organisation/Person/Outcome through the dedicated upserts
    AND routes generic labels (Concept/Framework/...) through
    ``upsert_node``. The integration test sees BOTH paths fire.
  - Re-running install against the same fixture is idempotent: the
    second pass calls ``upsert`` the same number of times AND every
    node id is the same (Neo4j MERGE → no duplicates server-side).
  - The status read path (parse nodes.json on disk) reflects the
    installed fixture's node count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kairix.knowledge.graph.models import GraphEdge
from kairix.knowledge.reflib.loader import load_entity_stubs

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _WritableFakeNeo4j:
    """Records every upsert call. Returns True so the loader counts
    each call as a successful load.

    Substitutes for ``kairix.knowledge.graph.client.Neo4jClient`` at
    the loader boundary. No real Neo4j connection required.
    """

    available: bool = True

    def __init__(self) -> None:
        self.organisations: list[Any] = []
        self.persons: list[Any] = []
        self.outcomes: list[Any] = []
        # (label, id, props) tuples for the generic path.
        self.generic_upserts: list[tuple[str, str, dict[str, Any]]] = []
        self.edges: list[GraphEdge] = []

    def upsert_organisation(self, node: Any) -> bool:
        self.organisations.append(node)
        return True

    def upsert_person(self, node: Any) -> bool:
        self.persons.append(node)
        return True

    def upsert_outcome(self, node: Any) -> bool:
        self.outcomes.append(node)
        return True

    def upsert_node(self, label: str, node_id: str, props: dict[str, Any]) -> bool:
        self.generic_upserts.append((label, node_id, dict(props)))
        return True

    def upsert_edge(self, edge: GraphEdge) -> bool:
        self.edges.append(edge)
        return True


def _write_fixture(reflib_root: Path) -> tuple[Path, Path]:
    """Write nodes.json + edges.json under reflib_root/entities/.

    Covers BOTH dispatch paths:
      - Organisation, Person, Outcome → dedicated upsert methods
      - Concept → generic ``upsert_node`` path
    """
    entities_dir = reflib_root / "entities"
    entities_dir.mkdir(parents=True)
    nodes_path = entities_dir / "nodes.json"
    edges_path = entities_dir / "edges.json"

    nodes_path.write_text(
        json.dumps(
            [
                {
                    "label": "Organisation",
                    "id": "acme-corp",
                    "name": "Acme Corp",
                    "vault_path": "entities/acme-corp.md",
                },
                {
                    "label": "Person",
                    "id": "alice-smith",
                    "name": "Alice Smith",
                    "vault_path": "entities/alice-smith.md",
                },
                {
                    "label": "Outcome",
                    "id": "outcome-1",
                    "name": "Outcome One",
                    "vault_path": "entities/outcome-1.md",
                },
                {
                    "label": "Concept",
                    "id": "concept-x",
                    "name": "Concept X",
                    "vault_path": "entities/concept-x.md",
                },
            ]
        ),
        encoding="utf-8",
    )
    edges_path.write_text(
        json.dumps(
            [
                {
                    "from_id": "alice-smith",
                    "from_label": "Person",
                    "to_id": "acme-corp",
                    "to_label": "Organisation",
                    "kind": "WORKS_AT",
                    "props": {},
                },
            ]
        ),
        encoding="utf-8",
    )
    return nodes_path, edges_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reflib_install_lands_dedicated_and_generic_upserts(tmp_path: Path) -> None:
    """One install pass routes Organisation/Person/Outcome through the
    dedicated upsert methods AND routes Concept through the generic
    ``upsert_node`` path. The report's counts match the fixture (4
    nodes loaded, 1 edge loaded, 0 errors).

    Sabotage: if ``_LABEL_DISPATCH`` lost the Outcome entry (or any of
    them), the matching list (e.g. ``client.outcomes``) would be empty
    and the assertion ``len(client.outcomes) == 1`` would fail.
    """
    reflib_root = tmp_path / "reflib"
    nodes_path, edges_path = _write_fixture(reflib_root)
    client = _WritableFakeNeo4j()

    report = load_entity_stubs(
        nodes_path=nodes_path,
        edges_path=edges_path,
        neo4j_client=client,
        dry_run=False,
    )

    # Report envelope.
    assert report.nodes_loaded == 4
    assert report.nodes_skipped == 0
    assert report.edges_loaded == 1
    assert report.edges_skipped == 0
    assert report.errors == []

    # Dedicated-dispatch paths.
    assert len(client.organisations) == 1
    assert client.organisations[0].id == "acme-corp"
    assert len(client.persons) == 1
    assert client.persons[0].id == "alice-smith"
    assert len(client.outcomes) == 1
    assert client.outcomes[0].id == "outcome-1"

    # Generic path — Concept went through upsert_node.
    assert len(client.generic_upserts) == 1
    label, node_id, props = client.generic_upserts[0]
    assert label == "Concept"
    assert node_id == "concept-x"
    assert props["name"] == "Concept X"

    # Edge landed.
    assert len(client.edges) == 1
    assert client.edges[0].from_id == "alice-smith"
    assert client.edges[0].to_id == "acme-corp"


def test_reflib_install_is_idempotent_no_duplicate_ids(tmp_path: Path) -> None:
    """Running install twice against the same fixture produces the same
    upsert COUNT each pass AND the same node ids — modelling Neo4j's
    MERGE semantics where a re-pass replays the upserts without
    creating duplicate server-side rows. Operators rely on this when
    iterating on extraction.

    Sabotage: if the loader started accumulating state across passes
    (e.g. caching node ids in a module-level list before upserting),
    the second pass would land DIFFERENT ids or skip them entirely —
    the symmetric-count and id-equality assertions catch that.
    """
    reflib_root = tmp_path / "reflib"
    nodes_path, edges_path = _write_fixture(reflib_root)

    client_pass_1 = _WritableFakeNeo4j()
    report_1 = load_entity_stubs(
        nodes_path=nodes_path,
        edges_path=edges_path,
        neo4j_client=client_pass_1,
        dry_run=False,
    )

    client_pass_2 = _WritableFakeNeo4j()
    report_2 = load_entity_stubs(
        nodes_path=nodes_path,
        edges_path=edges_path,
        neo4j_client=client_pass_2,
        dry_run=False,
    )

    # Reports symmetric.
    assert report_1.nodes_loaded == report_2.nodes_loaded == 4
    assert report_1.edges_loaded == report_2.edges_loaded == 1

    # Same id set both passes — MERGE-idempotent contract.
    ids_pass_1 = sorted(
        [n.id for n in client_pass_1.organisations]
        + [n.id for n in client_pass_1.persons]
        + [n.id for n in client_pass_1.outcomes]
        + [t[1] for t in client_pass_1.generic_upserts]
    )
    ids_pass_2 = sorted(
        [n.id for n in client_pass_2.organisations]
        + [n.id for n in client_pass_2.persons]
        + [n.id for n in client_pass_2.outcomes]
        + [t[1] for t in client_pass_2.generic_upserts]
    )
    assert ids_pass_1 == ids_pass_2
    # No duplicate ids within a single pass.
    assert len(ids_pass_1) == len(set(ids_pass_1)) == 4


def test_reflib_status_reflects_installed_fixture_on_disk(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``status`` is a disk read — it parses nodes.json/edges.json
    counts without re-running the loader. After install (which doesn't
    modify the source files), the status envelope reflects exactly the
    fixture's node/edge counts. This is the load-bearing operator
    feedback: "did install pick up everything I extracted?"

    Sabotage: if status started reading from a stale cached path
    instead of nodes_path on disk (or counted EDGES as NODES), the
    count assertions would diverge.

    Drives through the CLI's public ``main`` with ``--json`` so the
    integration exercises the production argparse → status_cmd path.
    """
    from kairix.knowledge.reflib.cli import main as reflib_main

    reflib_root = tmp_path / "reflib"
    nodes_path, edges_path = _write_fixture(reflib_root)
    client = _WritableFakeNeo4j()

    # Install once via the loader (CLI install needs a real Neo4jClient
    # factory we can't substitute through the public surface — but
    # status reads from disk, so it sees the fixture either way).
    load_entity_stubs(
        nodes_path=nodes_path,
        edges_path=edges_path,
        neo4j_client=client,
        dry_run=False,
    )

    # Read on-disk status via the CLI's --json envelope.
    with pytest.raises(SystemExit) as exc_info:
        reflib_main(["status", "--reflib-root", str(reflib_root), "--json"])
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    status = json.loads(captured.out)

    # Fixture had 4 nodes + 1 edge.
    assert status["entities_dir_exists"] is True
    assert status["node_count"] == 4
    assert status["edge_count"] == 1
    assert status["nodes_file"] == str(nodes_path)
    assert status["edges_file"] == str(edges_path)
    assert status["last_modified"] is not None
