"""Contract test for the GitHub connector plugin (F43).

Exercises the canonical fake (:class:`tests.fakes.FakeGitHubConnector`)
AND the real implementation
(:class:`kairix.connectors.github.GitHubConnector`) through the same
:class:`~kairix.core.protocols.SourceConnector` Protocol assertions.

F43 requires this pairing — without it the fake can drift away from the
real wire (or vice versa) and the production path silently diverges
from what BDD / unit tests measure.

Real-impl path is driven against a scripted :class:`_ScriptedClient`
satisfying the :class:`GitHubApiClient` shape — no real GitHub
roundtrip and no real secret resolution happens. The scripted client
is a contract-test-internal detail; the canonical fake from
``tests/fakes.py`` is what the production wiring imports.

Sabotage proof (executed by agent, restored on completion):

  * Removing the ``list_changes`` method from
    :class:`GitHubConnector` flips the real-impl isinstance check to
    False; deleting the corresponding attribute from
    :class:`FakeGitHubConnector` flips the fake check to False.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.connectors.github import GitHubConnector
from kairix.core.protocols import (
    ChangeEvent,
    Container,
    HierarchyConnector,
    HierarchyNode,
    RawArtefact,
    SourceConnector,
)
from tests.fakes import FakeGitHubConnector

_SEED_REPOS: list[dict[str, Any]] = [
    {
        "full_name": "agent-alpha-org/repo-alpha",
        "visibility": "private",
        "sha": "sha-alpha-1",
        "committed_at": "2026-05-23T01:00:00Z",
    }
]


class _ScriptedClient:
    """Internal scripted GitHubApiClient-shape collaborator."""

    def __init__(self, repos: list[dict[str, Any]]) -> None:
        from kairix.connectors.github.api_client import (
            ClientStatsSnapshot,
            GitHubRepoRef,
        )

        self._repos = tuple(
            GitHubRepoRef(
                repo_id=int(r.get("id", i + 1)),
                full_name=str(r["full_name"]),
                default_branch=str(r.get("default_branch", "main")),
                visibility=str(r.get("visibility", "private")),
                archived=bool(r.get("archived", False)),
            )
            for i, r in enumerate(repos)
        )
        self._commits = [
            {"sha": r["sha"], "committed_at": r["committed_at"], "full_name": r["full_name"]} for r in repos
        ]
        self._stats = ClientStatsSnapshot(
            rest_requests=0,
            rest_rate_remaining=5000,
            rest_rate_reset_epoch=0,
            rest_403_secondary_total=0,
            installation_token_rotations=0,
        )

    def list_installation_repositories(self):
        return self._repos

    def list_commits_since(self, *, full_name: str, since: str | None):
        from kairix.connectors.github.api_client import GitHubCommitRef

        _ = since
        out = []
        for c in self._commits:
            if c["full_name"] != full_name:
                continue
            out.append(
                GitHubCommitRef(sha=c["sha"], committed_at=c["committed_at"], message="seed", author="agent-alpha")
            )
        return tuple(out)

    def list_issues_since(self, *, full_name: str, since: str | None):
        _ = (full_name, since)
        return ()

    def get_tree_recursive(self, *, full_name: str, ref: str):
        _ = (full_name, ref)
        return (), False

    def fetch_blob(self, *, full_name: str, sha: str) -> bytes:
        _ = (full_name, sha)
        return b"blob-content"

    def stats(self):
        return self._stats

    def invalidate_token(self) -> None:
        return None


def _fake_factory() -> SourceConnector:
    """Canonical fake factory — pre-seeded repo / commit."""
    return FakeGitHubConnector(repos=list(_SEED_REPOS))


def _real_factory() -> SourceConnector:
    """Real-impl factory — drives the scripted client."""
    return GitHubConnector(client=_ScriptedClient(_SEED_REPOS))  # type: ignore[arg-type]  # F3 rationale: local stub mirrors GitHubApiClient shape but isn't typed as the Protocol — boundary-only suppression for the test seam


_FACTORIES: list[tuple[str, Callable[[], SourceConnector]]] = [
    ("fake", _fake_factory),
    ("real", _real_factory),
]


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_github_connector_satisfies_source_connector_protocol(
    name: str, factory: Callable[[], SourceConnector]
) -> None:
    """F43: both fake and real impl satisfy the runtime-checkable Protocol.

    Sabotage-proof: removing ``list_changes`` from
    :class:`GitHubConnector` flips the real-impl isinstance check to
    False; deleting the corresponding attribute from
    :class:`FakeGitHubConnector` flips the fake check to False.
    """
    connector = factory()
    assert isinstance(connector, SourceConnector), f"{name!r} factory output is not a SourceConnector"
    assert connector.name == "github"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_github_connector_list_changes_returns_change_events(name: str, factory: Callable[[], SourceConnector]) -> None:
    """Both implementations stream :class:`ChangeEvent` instances."""
    connector = factory()
    events = list(connector.list_changes(cursor=None))
    assert events, f"{name!r} factory produced no events"
    for ev in events:
        assert isinstance(ev, ChangeEvent), f"{name!r} yielded a non-ChangeEvent: {ev!r}"
        assert ev.op in ("created", "modified", "deleted", "archived", "access_lost")


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_github_connector_fetch_returns_raw_artefact(name: str, factory: Callable[[], SourceConnector]) -> None:
    """Both implementations satisfy the ``fetch`` → :class:`RawArtefact` shape."""
    connector = factory()
    events = list(connector.list_changes(cursor=None))
    assert events
    artefact = connector.fetch(events[0].item_id)
    assert isinstance(artefact, RawArtefact)
    assert artefact.fetched_at.endswith("Z") or "+" in artefact.fetched_at


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_github_connector_source_link_round_trips_to_github_com(
    name: str, factory: Callable[[], SourceConnector]
) -> None:
    """``source_link`` returns a ``github.com`` URL on both impls."""
    connector = factory()
    events = list(connector.list_changes(cursor=None))
    assert events
    link = connector.source_link(events[0].item_id)
    assert link.startswith("https://github.com/"), f"{name!r} produced unexpected link: {link!r}"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_github_connector_sensitivity_for_returns_configured_tier(
    name: str, factory: Callable[[], SourceConnector]
) -> None:
    """``sensitivity_for`` returns the F39 client-confidential default for private repos."""
    connector = factory()
    events = list(connector.list_changes(cursor=None))
    assert events
    tier = connector.sensitivity_for(events[0].item_id)
    assert tier == "client-confidential", f"{name!r} returned unexpected sensitivity: {tier!r}"


_TWO_REPOS: list[dict[str, Any]] = [
    {
        "full_name": "org-a/repo-1",
        "visibility": "private",
        "sha": "sha-1",
        "committed_at": "2026-05-23T01:00:00Z",
    },
    {
        "full_name": "org-b/repo-2",
        "visibility": "public",
        "sha": "sha-2",
        "committed_at": "2026-05-23T02:00:00Z",
    },
]


def _real_seeded(repos: list[dict[str, Any]]) -> Any:
    return GitHubConnector(client=_ScriptedClient(repos))  # type: ignore[arg-type]  # F3 rationale: local stub mirrors GitHubApiClient shape but isn't typed as the Protocol — boundary-only suppression for the test seam


# Factories over an explicit repo seed — same two repos on both impls.
_SEEDED_FACTORIES: list[tuple[str, Callable[[list[dict[str, Any]]], Any]]] = [
    ("fake", lambda repos: FakeGitHubConnector(repos=list(repos))),
    ("real", _real_seeded),
]


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _SEEDED_FACTORIES)
def test_github_connector_hierarchy_parent_before_child(
    name: str, factory: Callable[[list[dict[str, Any]]], Any]
) -> None:
    """F58: load_hierarchy emits parent-before-child (org → repo → dir) on
    both impls, and covers every seeded repo.

    Sabotage-proof: swapping the call order in
    :meth:`GitHubConnector.load_hierarchy` so repos emit before orgs
    flips the ``real`` case to fail at the first parent_id-not-in-emitted
    check. Restoring the canonical org-first / repos-second / dirs-third
    order returns the test to green.
    """
    connector = factory(_TWO_REPOS)
    assert isinstance(connector, HierarchyConnector), f"{name}: must satisfy HierarchyConnector"
    emitted: set[str] = set()
    nodes: list[HierarchyNode] = list(connector.load_hierarchy(cc_pair_id=42))
    assert nodes, f"{name}: load_hierarchy must emit at least one node"
    for node in nodes:
        if node.raw_parent_id is not None:
            assert node.raw_parent_id in emitted, (
                f"{name}: F58 violation: node {node.raw_node_id!r} parent_id "
                f"{node.raw_parent_id!r} was not previously emitted; emitted={sorted(emitted)!r}"
            )
        emitted.add(node.raw_node_id)
    assert {"github://org-a/repo-1", "github://org-b/repo-2"} <= emitted, f"{name}: {sorted(emitted)!r}"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _SEEDED_FACTORIES)
def test_github_connector_container_iteration_yields_one_per_repo(
    name: str, factory: Callable[[list[dict[str, Any]]], Any]
) -> None:
    """iter_containers emits one Container per installation-accessible repo.

    Sabotage proof (executed): in ``GitHubConnector.iter_containers`` yield
    only the first repo. Re-run: the ``real`` case fails. Restored.
    """
    containers = list(factory(_TWO_REPOS).iter_containers(cc_pair_id=99))
    assert len(containers) == 2, name
    assert {c.container_id for c in containers} == {"org-a/repo-1", "org-b/repo-2"}, name
    for container in containers:
        assert isinstance(container, Container)
        assert container.cc_pair_id == 99, name
