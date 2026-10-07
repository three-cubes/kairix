"""Contract test for the Gmail connector plugin (F43).

Exercises the canonical fake (:class:`tests.fakes.FakeGmailConnector`)
AND the real implementation
(:class:`kairix.connectors.gmail.GmailConnector`) through the SAME
:class:`~kairix.core.protocols.SourceConnector` /
``PollConnector`` / ``CheckpointedConnector`` assertions — every test body
below is parametrized over both.

F43 requires this pairing — without it the fake can drift away from the
real wire (or vice versa) and the production path silently diverges
from what BDD / unit tests measure.

Real-impl path is driven against a scripted ``_ScriptedGmailClient``
satisfying the :class:`GmailClient` shape via the public ``client=``
constructor seam — no real Gmail roundtrip and no real secret
resolution happens. Both impls are seeded from ONE message spec.

Findings (fake-vs-real drift, fixed in the fake — its only user is this
file): the fake used to (a) emit every message on a cold-start
``list_changes(None)`` where the real only seeds the cursor at the live
tip and emits nothing, (b) report a constant ``next_cursor`` where the
real reports ``None`` → profile tip → final historyId, (c) answer
``metadata_for`` before any drain where the real's envelope cache is
empty until a warm drain, and (d) ignore the checkpoint in
``load_from_checkpoint`` where the real forwards it to ``list_changes``.

Sabotage proofs are recorded per test (production code mutated, real
leg confirmed failing, restored).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.connectors.gmail import GmailConnector
from kairix.connectors.gmail.client import (
    GmailHeader,
    GmailMessage,
    HistoryPage,
)
from kairix.core.protocols import (
    ChangeEvent,
    CheckpointedConnector,
    Container,
    PollConnector,
    RawArtefact,
    SourceConnector,
    SourceMetadata,
)
from tests.fakes import FakeGmailConnector

_COLD_START_TIP = "cold-start-tip"
_FINAL_HISTORY_ID = "final-history-id"
_MAILBOX = "agent-alpha@example.com"


class _ScriptedGmailClient:
    """Internal scripted GmailClient-shape collaborator.

    Returns deterministic responses for History + Message calls so the
    real :class:`GmailConnector` can be exercised against the Protocol
    surface without any real Gmail roundtrip.
    """

    def __init__(self, *, messages: list[dict[str, Any]]) -> None:
        self._messages: list[GmailMessage] = []
        for entry in messages:
            header_values = {
                "Subject": entry["subject"],
                "From": entry["from"],
                "To": entry["to"],
                "Date": entry["date"],
            }
            headers = tuple(GmailHeader(name=k, value=v) for k, v in header_values.items())
            self._messages.append(
                GmailMessage(
                    message_id=str(entry["id"]),
                    thread_id=str(entry["thread_id"]),
                    history_id="1001",
                    label_ids=(),
                    headers=headers,
                    body=entry["body"],
                    body_mime="text/plain",
                    body_truncated=False,
                    attachments=(),
                )
            )
        self._by_id: dict[str, GmailMessage] = {m.message_id: m for m in self._messages}
        self._last_history_id: str | None = None

    def get_profile_history_id(self) -> str:
        return _COLD_START_TIP

    def list_history(self, *, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        _ = (start_history_id, page_token)
        return HistoryPage(
            message_ids=tuple(m.message_id for m in self._messages),
            next_page_token=None,
            history_id=_FINAL_HISTORY_ID,
        )

    def iter_history_message_ids(self, *, start_history_id: str) -> Any:
        _ = start_history_id
        self._last_history_id = _FINAL_HISTORY_ID
        for message in self._messages:
            yield message.message_id

    def last_history_id(self) -> str | None:
        return self._last_history_id

    def get_message(self, message_id: str) -> GmailMessage:
        return self._by_id[message_id]

    def stats(self) -> Any:
        from kairix.connectors.gmail.client import GmailStatsSnapshot

        return GmailStatsSnapshot(requests=0, rate_limited_403_total=0, token_refreshes=0)

    def invalidate_token(self) -> None:
        return None


def _message(msg_id: str = "gm-msg-1", thread_id: str = "gm-thread-1") -> dict[str, Any]:
    """One message spec both impls are seeded from."""
    return {
        "id": msg_id,
        "thread_id": thread_id,
        "from": "agent-alpha@example.com",
        "to": "agent-beta@example.com",
        "subject": "Project update",
        "date": "2026-05-28T09:00:00Z",
        "body": b"Body of project update.",
    }


def _build_real(messages: list[dict[str, Any]]) -> Any:
    client = _ScriptedGmailClient(messages=messages)
    return GmailConnector(user_email=_MAILBOX, client=client)  # type: ignore[arg-type]  # F3 rationale: local stub mirrors GmailClient shape but isn't typed as the Protocol — boundary-only suppression for the test seam


def _build_fake(messages: list[dict[str, Any]]) -> Any:
    return FakeGmailConnector(
        user_email=_MAILBOX,
        messages=messages,
        profile_history_id=_COLD_START_TIP,
        final_history_id=_FINAL_HISTORY_ID,
    )


_Builder = Callable[[list[dict[str, Any]]], Any]
_BUILDERS: list[_Builder] = [_build_fake, _build_real]
_IDS = ["fake", "real"]


def _drained(build: _Builder) -> Any:
    """Build from the seed message and drive a cold-start + warm tick so
    the connector cache is populated for fetch() / metadata_for()."""
    connector = build([_message()])
    list(connector.list_changes(cursor=None))
    list(connector.list_changes(cursor=_COLD_START_TIP))
    return connector


_DRAINED: list[Callable[[], Any]] = [lambda: _drained(_build_fake), lambda: _drained(_build_real)]

_FACTORIES: list[tuple[str, Callable[[], SourceConnector]]] = [
    ("fake", lambda: _drained(_build_fake)),
    ("real", lambda: _drained(_build_real)),
]


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_gmail_connector_satisfies_source_connector_protocol(name: str, factory: Callable[[], SourceConnector]) -> None:
    """F43: both fake and real impl satisfy the runtime-checkable Protocol.

    Sabotage-proof: removing ``list_changes`` from
    :class:`GmailConnector` flips the real-impl isinstance check to
    False; deleting the corresponding attribute from
    :class:`FakeGmailConnector` flips the fake check to False.
    """
    connector = factory()
    assert isinstance(connector, SourceConnector), f"{name!r} factory output is not a SourceConnector"
    assert connector.name == "gmail"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_gmail_connector_pair_source_link_round_trips(name: str, factory: Callable[[], SourceConnector]) -> None:
    """Both implementations round-trip source_link to a mail.google.com URL."""
    connector = factory()
    link = connector.source_link("gm-msg-1")
    assert link.startswith("https://mail.google.com/mail/"), f"{name!r} produced unexpected link: {link!r}"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_gmail_connector_pair_sensitivity_for_returns_client_confidential(
    name: str, factory: Callable[[], SourceConnector]
) -> None:
    """Both implementations default to ``client-confidential`` per the Gmail spec.

    (Absorbs the former real-only ``..._sensitivity_for_returns_default_tier``
    test — same assertion, now run over both impls.)
    """
    connector = factory()
    tier = connector.sensitivity_for("gm-msg-1")
    assert tier == "client-confidential", f"{name!r} produced unexpected sensitivity: {tier!r}"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _DRAINED, ids=_IDS)
def test_gmail_connector_satisfies_capability_protocols(factory: Callable[[], Any]) -> None:
    """The connector satisfies the Poll + Checkpointed capability Protocols (F56).

    Sabotage proof: renamed ``GmailConnector.load_from_checkpoint`` to
    ``_load_from_checkpoint`` → the real leg's CheckpointedConnector check
    failed. Restored.
    """
    connector = factory()
    assert isinstance(connector, PollConnector), "Gmail connector must satisfy PollConnector"
    assert isinstance(connector, CheckpointedConnector), "Gmail connector must satisfy CheckpointedConnector"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_list_changes_returns_change_events(factory: _Builder) -> None:
    """Cold start (``cursor=None``) seeds the cursor and emits nothing; the
    warm tick yields one ``created`` :class:`ChangeEvent` per message,
    stamped with the mailbox sensitivity.

    Sabotage proof: in ``GmailConnector.list_changes`` removed the
    cold-start ``return iter(events)`` so a ``None`` cursor fell through to
    the History drain → the real leg's cold-start ``== []`` failed. Restored.
    """
    connector = factory([_message("gm-msg-x", "gm-thread-x")])
    assert list(connector.list_changes(cursor=None)) == [], "cold start must only seed the cursor"
    events = list(connector.list_changes(cursor=_COLD_START_TIP))
    assert events, "warm tick must produce events from the scripted page"
    for ev in events:
        assert isinstance(ev, ChangeEvent)
        assert ev.op == "created"
        assert ev.item_id == "gm-msg-x"
        assert ev.modified_at == "2026-05-28T09:00:00Z"
        assert ev.metadata.get("sensitivity") == "client-confidential"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _DRAINED, ids=_IDS)
def test_gmail_connector_fetch_returns_raw_artefact(factory: Callable[[], Any]) -> None:
    """fetch returns a :class:`RawArtefact` from the cached message body.

    Sabotage proof: in ``GmailConnector.fetch`` returned ``raw=b""`` → the
    real leg's body assertion failed. Restored.
    """
    connector = factory()
    artefact = connector.fetch("gm-msg-1")
    assert isinstance(artefact, RawArtefact)
    assert artefact.raw == b"Body of project update."
    assert artefact.mime == "text/plain"
    assert artefact.fetched_at.endswith("Z") or "+" in artefact.fetched_at


@pytest.mark.contract
@pytest.mark.parametrize("factory", _DRAINED, ids=_IDS)
def test_gmail_connector_source_link_round_trips_to_gmail_web_inbox(factory: Callable[[], Any]) -> None:
    """source_link returns a mail.google.com URL carrying the message id.

    Sabotage proof: in ``GmailConnector.source_link`` dropped the
    ``quote(item_id, ...)`` suffix → the real leg's id-in-link assertion
    failed. Restored.
    """
    connector = factory()
    link = connector.source_link("gm-msg-1")
    assert link.startswith("https://mail.google.com/mail/u/0/#inbox/")
    assert "gm-msg-1" in link


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_next_cursor_advances_after_drain(factory: _Builder) -> None:
    """next_cursor is ``None`` before any tick, the live profile tip after a
    cold start, and the final historyId after a successful warm drain.

    Sabotage proof: in ``GmailConnector.list_changes`` replaced
    ``self._next_cursor = self._client.last_history_id()`` with
    ``self._next_cursor = cursor`` → the real leg's final-history-id
    assertion failed. Restored.
    """
    connector = factory([_message()])
    assert connector.next_cursor() is None
    list(connector.list_changes(cursor=None))
    assert connector.next_cursor() == _COLD_START_TIP
    list(connector.list_changes(cursor=_COLD_START_TIP))
    cursor = connector.next_cursor()
    assert cursor == _FINAL_HISTORY_ID, f"next_cursor must surface the final historyId; got {cursor!r}"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _DRAINED, ids=_IDS)
def test_gmail_connector_metadata_for_surfaces_envelope_headers(factory: Callable[[], Any]) -> None:
    """metadata_for lifts Subject / From / To / Date from the cached headers (F65).

    Sabotage proof: in ``GmailConnector.metadata_for`` dropped the
    ``properties["thread_id"] = ...`` line → the real leg's thread_id
    assertion failed. Restored.
    """
    connector = factory()
    metadata = connector.metadata_for("gm-msg-1")
    assert isinstance(metadata, SourceMetadata)
    assert metadata.author == "agent-alpha@example.com"
    assert metadata.author_email == "agent-alpha@example.com"
    assert metadata.modified_at == "2026-05-28T09:00:00Z"
    assert "agent-beta@example.com" in metadata.tags
    assert metadata.properties.get("subject") == "Project update"
    assert metadata.properties.get("thread_id") == "gm-thread-1"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_metadata_for_missing_item_returns_empty(factory: _Builder) -> None:
    """metadata_for returns an empty :class:`SourceMetadata` on cache miss —
    both for an id never seen and for a known id before any warm drain.

    Sabotage proof: in ``GmailConnector.metadata_for`` replaced the
    cache-miss ``return SourceMetadata()`` with an on-demand
    ``self._client.get_message(item_id)`` lookup → the real leg surfaced
    the undrained envelope and failed. Restored.
    """
    connector = factory([_message()])
    for item_id in ("gm-msg-1", "never-seen"):
        metadata = connector.metadata_for(item_id)
        assert metadata.author is None
        assert metadata.tags == ()
        assert dict(metadata.properties) == {}


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_iter_containers_yields_one_per_mailbox(factory: _Builder) -> None:
    """iter_containers emits exactly one Container for the configured mailbox.

    Sabotage proof: in ``GmailConnector.iter_containers`` set
    ``access_state="INACCESSIBLE"`` → the real leg failed. Restored.
    """
    connector = factory([_message("gm-msg-cap")])
    containers = list(connector.iter_containers(cc_pair_id=42))
    assert len(containers) == 1
    assert containers[0].cc_pair_id == 42
    assert containers[0].container_id == _MAILBOX
    assert containers[0].access_state == "ACCESSIBLE"


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_load_hierarchy_emits_single_root_folder(factory: _Builder) -> None:
    """load_hierarchy emits one root FOLDER for the mailbox (Wave E shim).

    Sabotage proof: in ``GmailConnector.load_hierarchy`` changed
    ``node_type="FOLDER"`` to ``"SPACE"`` → the real leg failed. Restored.
    """
    connector = factory([_message("gm-msg-cap")])
    nodes = list(connector.load_hierarchy(cc_pair_id=42))
    assert len(nodes) == 1
    root = nodes[0]
    assert root.raw_parent_id is None
    assert root.cc_pair_id == 42
    assert root.node_type == "FOLDER"
    assert "Gmail" in root.display_name


@pytest.mark.contract
@pytest.mark.parametrize("factory", _BUILDERS, ids=_IDS)
def test_gmail_connector_load_from_checkpoint_delegates_to_list_changes(factory: _Builder) -> None:
    """load_from_checkpoint is the CheckpointedConnector shim — forwards
    the checkpoint string into list_changes (a ``None`` checkpoint is a
    cold start that emits nothing).

    Sabotage proof: in ``GmailConnector.load_from_checkpoint`` returned
    ``self.list_changes(None)`` → the real leg's warm-checkpoint drain was
    empty and failed. Restored.
    """
    connector = factory([_message("gm-msg-ck")])
    container = Container(
        cc_pair_id=1,
        container_id=_MAILBOX,
        access_state="ACCESSIBLE",
        cursor_token="seed",
        last_synced_at=None,
    )
    assert list(connector.load_from_checkpoint(container, None)) == []
    events = list(connector.load_from_checkpoint(container, "warm-checkpoint"))
    assert events, "load_from_checkpoint must surface events from the underlying list_changes drain"
    assert events[0].item_id == "gm-msg-ck"
