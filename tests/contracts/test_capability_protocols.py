"""Contract tests for the topology Wave B capability mix-in Protocols.

Each of the 9 capability Protocols added in Wave B
(PollConnector, CheckpointedConnector, SlimConnector,
SlimConnectorWithPermSync, EventConnector, Resolver, HierarchyConnector,
OAuthConnector, CredentialsConnector) gets:

  1. A canonical Fake satisfies ``isinstance(fake, ProtocolClass)`` via
     the ``@runtime_checkable`` decorator.
  2. At least one shipped connector satisfies the relevant Protocols
     per the capability matrix from
     docs/architecture/connector-scope-topology/ADR.md §"Wave B".

Per F46/F47 conventions, these tests construct connectors directly
(allowed in tests/contracts/ — see CLAUDE.md §"How to test"). They
exercise Protocol shape only — behavioural tests for each shim live
alongside the connector's own contract test (test_<name>_protocol.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.connectors.dex_crm import DexCrmConnector
from kairix.connectors.m365_calendar import M365CalendarConnector
from kairix.connectors.m365_calendar.connector import M365CalendarConfig
from kairix.connectors.m365_email_headers import M365EmailHeadersConnector
from kairix.connectors.m365_email_headers.connector import M365Credentials
from kairix.connectors.obsidian.connector import ObsidianConnector
from kairix.core.protocols import (
    CheckpointedConnector,
    CredentialsConnector,
    EventConnector,
    HierarchyConnector,
    OAuthConnector,
    PollConnector,
    Resolver,
    SlimConnector,
    SlimConnectorWithPermSync,
    SourceConnector,
)
from tests.fakes import (
    FakeCheckpointedConnector,
    FakeEventConnector,
    FakeHierarchyConnector,
    FakePollConnector,
    FakeResolver,
    FakeSlimConnector,
    FakeSlimConnectorWithPermSync,
    fake_obsidian_watcher_factory,
)

pytestmark = pytest.mark.contract


# Module-level tuple of every capability Protocol class — referenced by
# the per-connector capability-inventory contract tests below and by the
# F56 runtime probe.
CAPABILITY_PROTOCOLS: tuple[type, ...] = (
    SourceConnector,
    PollConnector,
    CheckpointedConnector,
    SlimConnector,
    SlimConnectorWithPermSync,
    EventConnector,
    Resolver,
    HierarchyConnector,
    OAuthConnector,
    CredentialsConnector,
)


# ---------------------------------------------------------------------------
# Fake satisfies Protocol — runtime isinstance shape proofs
# ---------------------------------------------------------------------------


def test_fake_poll_connector_satisfies_protocol() -> None:
    assert isinstance(FakePollConnector(), PollConnector)


def test_fake_checkpointed_connector_satisfies_protocol() -> None:
    assert isinstance(FakeCheckpointedConnector(), CheckpointedConnector)


def test_fake_slim_connector_satisfies_protocol() -> None:
    assert isinstance(FakeSlimConnector(), SlimConnector)


def test_fake_slim_connector_with_perm_sync_satisfies_protocol() -> None:
    assert isinstance(FakeSlimConnectorWithPermSync(), SlimConnectorWithPermSync)


def test_fake_event_connector_satisfies_protocol() -> None:
    assert isinstance(FakeEventConnector(), EventConnector)


def test_fake_resolver_satisfies_protocol() -> None:
    assert isinstance(FakeResolver(), Resolver)


def test_fake_hierarchy_connector_satisfies_protocol() -> None:
    assert isinstance(FakeHierarchyConnector(), HierarchyConnector)


# OAuthConnector + CredentialsConnector share the SourceConnector fakes
# via extension (their methods don't need observable state — the shipped
# connectors satisfy them by inheritance / shim per the matrix).


# ---------------------------------------------------------------------------
# Real connector satisfies relevant capability Protocols — per matrix
# ---------------------------------------------------------------------------


def _obsidian(tmp_path: Path) -> ObsidianConnector:
    vault = tmp_path / "vault"
    vault.mkdir()
    return ObsidianConnector(vault_root=vault)


def _dex_crm() -> DexCrmConnector:
    return DexCrmConnector()


def _m365_email_headers() -> M365EmailHeadersConnector:
    return M365EmailHeadersConnector(
        user_principal_name="probe@example.com",
        credentials=M365Credentials(tenant_id="t", client_id="c", client_secret="s"),
    )


def _m365_calendar() -> M365CalendarConnector:
    return M365CalendarConnector(M365CalendarConfig(user_id="u", tenant_id="t", client_id="c", client_secret="s"))


def test_obsidian_satisfies_poll_connector(tmp_path: Path) -> None:
    """ObsidianConnector advertises PollConnector via the Wave B shim."""
    assert isinstance(_obsidian(tmp_path), PollConnector)


def test_obsidian_satisfies_slim_connector(tmp_path: Path) -> None:
    """ObsidianConnector advertises SlimConnector via the Wave B shim."""
    assert isinstance(_obsidian(tmp_path), SlimConnector)


def test_obsidian_satisfies_hierarchy_connector(tmp_path: Path) -> None:
    """ObsidianConnector advertises HierarchyConnector via the Wave B shim."""
    assert isinstance(_obsidian(tmp_path), HierarchyConnector)


def test_dex_crm_satisfies_poll_connector() -> None:
    assert isinstance(_dex_crm(), PollConnector)


def test_dex_crm_satisfies_credentials_connector() -> None:
    assert isinstance(_dex_crm(), CredentialsConnector)


def test_m365_email_headers_satisfies_checkpointed_connector() -> None:
    assert isinstance(_m365_email_headers(), CheckpointedConnector)


def test_m365_email_headers_satisfies_credentials_connector() -> None:
    assert isinstance(_m365_email_headers(), CredentialsConnector)


def test_m365_email_headers_satisfies_oauth_connector() -> None:
    assert isinstance(_m365_email_headers(), OAuthConnector)


def test_m365_calendar_satisfies_checkpointed_connector() -> None:
    assert isinstance(_m365_calendar(), CheckpointedConnector)


def test_m365_calendar_satisfies_credentials_connector() -> None:
    assert isinstance(_m365_calendar(), CredentialsConnector)


def test_m365_calendar_satisfies_oauth_connector() -> None:
    assert isinstance(_m365_calendar(), OAuthConnector)


# ---------------------------------------------------------------------------
# Per-connector capability-inventory contract tests
# ---------------------------------------------------------------------------
#
# Each shipped connector's expected capability set per the Wave B matrix.
# The actual set is computed by runtime isinstance() across
# CAPABILITY_PROTOCOLS; the expected set must be a subset.


def test_obsidian_capability_set(tmp_path: Path) -> None:
    """ObsidianConnector satisfies SourceConnector + PollConnector + SlimConnector + HierarchyConnector."""
    obsidian = _obsidian(tmp_path)
    expected = {SourceConnector, PollConnector, SlimConnector, HierarchyConnector}
    actual = {p for p in CAPABILITY_PROTOCOLS if isinstance(obsidian, p)}
    missing = expected - actual
    assert not missing, f"obsidian missing capabilities: {sorted(p.__name__ for p in missing)}"


def test_dex_crm_capability_set() -> None:
    """DexCrmConnector satisfies SourceConnector + PollConnector + CredentialsConnector."""
    dex = _dex_crm()
    expected = {SourceConnector, PollConnector, CredentialsConnector}
    actual = {p for p in CAPABILITY_PROTOCOLS if isinstance(dex, p)}
    missing = expected - actual
    assert not missing, f"dex_crm missing capabilities: {sorted(p.__name__ for p in missing)}"


def test_m365_email_headers_capability_set() -> None:
    """M365EmailHeadersConnector capability set per the Wave B matrix.

    Satisfies: SourceConnector + CheckpointedConnector +
    CredentialsConnector + OAuthConnector.
    """
    conn = _m365_email_headers()
    expected = {SourceConnector, CheckpointedConnector, CredentialsConnector, OAuthConnector}
    actual = {p for p in CAPABILITY_PROTOCOLS if isinstance(conn, p)}
    missing = expected - actual
    assert not missing, f"m365_email_headers missing capabilities: {sorted(p.__name__ for p in missing)}"


def test_m365_calendar_capability_set() -> None:
    """M365CalendarConnector capability set per the Wave B matrix.

    Satisfies: SourceConnector + CheckpointedConnector +
    CredentialsConnector + OAuthConnector.
    """
    conn = _m365_calendar()
    expected = {SourceConnector, CheckpointedConnector, CredentialsConnector, OAuthConnector}
    actual = {p for p in CAPABILITY_PROTOCOLS if isinstance(conn, p)}
    missing = expected - actual
    assert not missing, f"m365_calendar missing capabilities: {sorted(p.__name__ for p in missing)}"


# ---------------------------------------------------------------------------
# Shim behavioural smoke — delegation correctness
# ---------------------------------------------------------------------------


def test_obsidian_load_hierarchy_yields_one_root_folder(tmp_path: Path) -> None:
    """The HierarchyConnector shim yields exactly one root FOLDER node."""
    vault = tmp_path / "agent-alpha-vault"
    vault.mkdir()
    conn = ObsidianConnector(vault_root=vault)
    nodes = list(conn.load_hierarchy(cc_pair_id=42))
    assert len(nodes) == 1
    assert nodes[0].cc_pair_id == 42
    assert nodes[0].node_type == "FOLDER"
    assert nodes[0].raw_parent_id is None
    assert nodes[0].display_name == "agent-alpha-vault"


def test_dex_crm_load_credentials_passes_through_unchanged() -> None:
    """The CredentialsConnector shim returns the input unchanged."""
    conn = DexCrmConnector()
    raw = {"api_key": "abc", "extra": "value"}
    result = conn.load_credentials(raw)
    assert result == raw


def test_m365_email_headers_oauth_auth_url_raises_with_fix_marker() -> None:
    """The OAuthConnector shim raises NotImplementedError with an actionable fix marker.

    Positional invocation — the implementation underscore-prefixes the
    unused parameter per F19, so kwarg invocation is not part of the
    contract.
    """
    with pytest.raises(NotImplementedError, match=r"fix:"):
        M365EmailHeadersConnector.oauth_authorization_url("agent-alpha-state")


def test_m365_calendar_oauth_code_to_token_raises_with_fix_marker() -> None:
    """The OAuthConnector shim raises NotImplementedError with an actionable fix marker."""
    with pytest.raises(NotImplementedError, match=r"fix:"):
        M365CalendarConnector.oauth_code_to_token("agent-alpha-code")


# ---------------------------------------------------------------------------
# Shim delegation coverage — exercise the new methods so the per-file
# coverage floor stays clean (F7 90% per-file)
# ---------------------------------------------------------------------------


def test_obsidian_list_changes_for_container_delegates_to_list_changes(tmp_path: Path) -> None:
    """The PollConnector shim forwards ``container.cursor_token`` to ``list_changes``.

    Construct a container with an empty cursor token; on a fresh vault
    the iterator should yield zero events (no markdown files present)
    matching what the legacy ``list_changes(None)`` returns.
    """
    from kairix.core.protocols import Container

    vault = tmp_path / "agent-alpha-vault"
    vault.mkdir()
    # list_changes_for_container starts the watcher — keep it in-process (no OS thread).
    conn = ObsidianConnector(vault_root=vault, watcher_factory=fake_obsidian_watcher_factory())
    container = Container(
        cc_pair_id=1,
        container_id="default",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    events = list(conn.list_changes_for_container(container))
    # No markdown files on a fresh vault → no events
    assert events == []


def test_obsidian_retrieve_all_slim_docs_yields_item_ids(tmp_path: Path) -> None:
    """The SlimConnector shim walks the reconciler and yields item_id strings.

    Seed the vault with one markdown file so the reconciler emits one
    event; the shim must yield the corresponding item_id.
    """
    from kairix.core.protocols import Container

    vault = tmp_path / "agent-beta-vault"
    vault.mkdir()
    (vault / "note.md").write_text("# hello\n", encoding="utf-8")
    conn = ObsidianConnector(vault_root=vault)
    container = Container(
        cc_pair_id=1,
        container_id="default",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    ids = list(conn.retrieve_all_slim_docs(container))
    assert "note.md" in ids


def test_dex_crm_list_changes_for_container_delegates() -> None:
    """The dex_crm PollConnector shim forwards container.cursor_token to list_changes.

    Uses a real DexCrmClient with httpx.MockTransport returning empty
    listings (no creds needed for the recording transport path) — the
    test asserts the shim builds an iterator and yields zero events,
    proving the delegation path is exercised.
    """
    import httpx

    from kairix.connectors.dex_crm.client import DexCrmClient, DexCrmClientConfig
    from kairix.core.protocols import Container
    from kairix.transport.auth.api_key import ApiKeyAuth, BearerHeaders

    class _ScriptedAuth(ApiKeyAuth):
        def headers(self, _secret_name: str) -> BearerHeaders:
            return BearerHeaders(mapping={"Authorization": "Bearer x"})

    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    client = DexCrmClient(
        config=DexCrmClientConfig(rate_limit_sleep_s=0.0),
        http_client=httpx.Client(transport=httpx.MockTransport(_handler)),
        auth=_ScriptedAuth(),
        sleep=lambda _s: None,
    )
    conn = DexCrmConnector(client=client)
    container = Container(
        cc_pair_id=1,
        container_id="default",
        access_state="ACCESSIBLE",
        cursor_token=None,
        last_synced_at=None,
    )
    events = list(conn.list_changes_for_container(container))
    assert events == []


def test_m365_email_headers_load_credentials_passes_through_unchanged() -> None:
    """The m365_email_headers CredentialsConnector shim returns input unchanged."""
    conn = _m365_email_headers()
    raw: dict[str, Any] = {"tenant_id": "t", "extra": "v"}
    assert conn.load_credentials(raw) == raw


def test_m365_calendar_load_credentials_passes_through_unchanged() -> None:
    """The m365_calendar CredentialsConnector shim returns input unchanged."""
    conn = _m365_calendar()
    raw: dict[str, Any] = {"tenant_id": "t", "extra": "v"}
    assert conn.load_credentials(raw) == raw
