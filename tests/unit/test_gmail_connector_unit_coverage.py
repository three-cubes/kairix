"""Unit coverage tests for the Gmail connector (GH #359).

Focused per-function coverage of the smaller branches in
:mod:`kairix.connectors.gmail.connector` that the integration / contract
tests don't exercise. F7 (per-file >=90% coverage) is the gate this
file pays down for the new Gmail connector code.

Every test drives behaviour through the **public** boundary:

* Body-decode / metadata-projection helpers are exercised by driving
  the connector's public ``metadata_for`` against a scripted
  :class:`GmailMessage` envelope. No reach into private symbols (F5).

* ``list_changes_for_container`` OFF + ON branches drive the Wave E
  per-mailbox shim through the public Container surface with an
  injected ``flag_reader`` callable. No monkey-patch on the registry.

* The default client builder is exercised implicitly by constructing
  ``GmailConnector(user_email, credentials=...)`` without ``client=``
  so the production builder path fires.

* ``make_connector`` validation branches drive the YAML-config surface
  with bad / good shapes; happy-path construction is exercised by
  threading credentials through a per-file XDG secret directory so the
  production path resolves end-to-end without monkey-patching kairix
  internals.

F1-clean: no @patch or kairix module-attribute substitution.
F2-clean: no KAIRIX_* env-var manipulation.
F5-clean: every test reaches behaviour via the public boundary.
F8: ``pytestmark = pytest.mark.unit``.
F31/F32: no real names or local-machine paths; only ``agent-*@example.com``
fixtures and pytest ``tmp_path``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kairix.connectors.gmail import (
    GmailAttachment,
    GmailConnector,
    GmailCredentials,
    GmailHeader,
    GmailMessage,
    HistoryPage,
    make_connector,
)
from kairix.core.protocols import Container

pytestmark = pytest.mark.unit


_USER = "agent-alpha@example.com"


# ---------------------------------------------------------------------------
# Fake GmailClient stand-in for connector-level branches
# ---------------------------------------------------------------------------


class _FakeGmailClient:
    """Scripted GmailClient stand-in.

    Supports tuning per-call behaviour so unit tests can drive the
    connector's branches deterministically without a real Gmail call.
    """

    def __init__(
        self,
        *,
        profile_history_id: str = "tip-1",
        messages: list[GmailMessage] | None = None,
        history_pages: list[HistoryPage] | None = None,
        terminal_history_id: str | None = "final-tip",
    ) -> None:
        self._profile_history_id = profile_history_id
        self._messages: dict[str, GmailMessage] = {m.message_id: m for m in (messages or [])}
        self._history_pages = history_pages or []
        self._terminal_history_id = terminal_history_id
        self._iter_calls: list[str] = []
        self._get_calls: list[str] = []
        self._last_history_id: str | None = None

    def get_profile_history_id(self) -> str:
        return self._profile_history_id

    def list_history(self, *, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        _ = (start_history_id, page_token)
        if self._history_pages:
            return self._history_pages[0]
        return HistoryPage(message_ids=(), next_page_token=None, history_id=None)

    def iter_history_message_ids(self, *, start_history_id: str) -> Iterator[str]:
        self._iter_calls.append(start_history_id)
        # Yield every queued message id; record terminal historyId for last_history_id.
        self._last_history_id = self._terminal_history_id
        yield from self._messages

    def last_history_id(self) -> str | None:
        return self._last_history_id

    def get_message(self, message_id: str) -> GmailMessage:
        self._get_calls.append(message_id)
        return self._messages[message_id]

    def stats(self) -> Any:
        from kairix.connectors.gmail.client import GmailStatsSnapshot

        return GmailStatsSnapshot(requests=0, rate_limited_403_total=0, token_refreshes=0)

    def invalidate_token(self) -> None:
        return None


def _make_message(
    message_id: str = "msg-1",
    *,
    headers: tuple[GmailHeader, ...] | None = None,
    body: bytes = b"body",
    label_ids: tuple[str, ...] = ("INBOX",),
    thread_id: str = "thread-1",
    attachments: tuple[GmailAttachment, ...] = (),
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id=thread_id,
        history_id="1000",
        label_ids=label_ids,
        headers=headers or (),
        body=body,
        body_mime="text/plain",
        body_truncated=False,
        attachments=attachments,
    )


# ---------------------------------------------------------------------------
# Constructor / DI seam validation
# ---------------------------------------------------------------------------


def test_connector_rejects_empty_user_email_with_fix_pointer() -> None:
    """Empty ``user_email`` raises with an actionable error.

    Sabotage proof: deleting the ``if not user_email`` guard in
    ``GmailConnector.__init__`` lets the empty-mailbox configuration
    pass through silently — this test would catch the regression.
    """
    with pytest.raises(ValueError) as exc_info:
        GmailConnector(user_email="")
    msg = str(exc_info.value)
    assert "user_email" in msg
    assert "fix:" in msg
    assert "next:" in msg


def test_connector_default_per_tick_max_items_matches_class_attr() -> None:
    """Per-tick max items defaults to 500 (Gmail-spec batching).

    Sabotage proof: changing the class attribute to a different number
    would flip this assertion.
    """
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    assert connector.per_tick_max_items == 500
    assert connector.disk_watermark_min_free_bytes == 5_000_000_000


# ---------------------------------------------------------------------------
# fetch() cache-miss path (line 337)
# ---------------------------------------------------------------------------


def test_fetch_cache_miss_calls_client_and_populates_cache() -> None:
    """Fetch with no prior list_changes triggers a direct ``get_message`` lookup.

    Sabotage proof: removing the cache-miss branch in :meth:`fetch`
    (returning empty bytes on miss) would surface as a wrong body
    payload.
    """
    msg = _make_message("orphan-msg", body=b"orphan body")
    client = _FakeGmailClient(messages=[msg])
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    artefact = connector.fetch("orphan-msg")
    assert artefact.raw == b"orphan body"
    assert client._get_calls == ["orphan-msg"]
    # Subsequent fetch must hit the warmed cache — no second get_message.
    artefact2 = connector.fetch("orphan-msg")
    assert artefact2.raw == b"orphan body"
    assert client._get_calls == ["orphan-msg"]


def test_fetch_uses_default_mime_when_message_body_mime_empty() -> None:
    """When message.body_mime is empty, fetch surfaces ``text/plain`` per spec."""
    msg = GmailMessage(
        message_id="m1",
        thread_id="t1",
        history_id="1",
        label_ids=(),
        headers=(),
        body=b"x",
        body_mime="",  # empty triggers fallback
        body_truncated=False,
        attachments=(),
    )
    client = _FakeGmailClient(messages=[msg])
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    artefact = connector.fetch("m1")
    assert artefact.mime == "text/plain"


# ---------------------------------------------------------------------------
# metadata_for: bcc + attachments + label branches (390, 394)
# ---------------------------------------------------------------------------


def test_metadata_for_surfaces_bcc_when_present() -> None:
    """``Bcc`` header lands as a ``bcc`` property on the envelope.

    Sabotage proof: dropping the ``if bcc_addrs`` branch in metadata_for
    would silently lose Bcc context — this test catches it.
    """
    msg = _make_message(
        "with-bcc",
        headers=(
            GmailHeader(name="From", value="agent-alpha@example.com"),
            GmailHeader(name="To", value="agent-beta@example.com"),
            GmailHeader(name="Bcc", value="agent-gamma@example.com, agent-delta@example.com"),
            GmailHeader(name="Date", value="2026-05-28T10:00:00Z"),
        ),
    )
    client = _FakeGmailClient(
        messages=[msg],
        history_pages=[HistoryPage(message_ids=("with-bcc",), next_page_token=None, history_id="t")],
    )
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    list(connector.list_changes(cursor=None))
    list(connector.list_changes(cursor="cold-tip"))
    md = connector.metadata_for("with-bcc")
    assert md.properties.get("bcc") == "agent-gamma@example.com, agent-delta@example.com"


def test_metadata_for_surfaces_attachments_filenames_when_present() -> None:
    """Attachment filenames join into the ``attachments`` property.

    Sabotage proof: dropping the ``if message.attachments`` branch in
    metadata_for would lose attachment context on chunks.
    """
    msg = _make_message(
        "with-attach",
        headers=(GmailHeader(name="From", value="agent-alpha@example.com"),),
        attachments=(
            GmailAttachment(filename="report.pdf", mime_type="application/pdf", size_bytes=100, attachment_id="a1"),
            GmailAttachment(filename="data.csv", mime_type="text/csv", size_bytes=50, attachment_id="a2"),
        ),
    )
    client = _FakeGmailClient(
        messages=[msg],
        history_pages=[HistoryPage(message_ids=("with-attach",), next_page_token=None, history_id="t")],
    )
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    list(connector.list_changes(cursor=None))
    list(connector.list_changes(cursor="cold-tip"))
    md = connector.metadata_for("with-attach")
    assert md.properties.get("attachments") == "report.pdf, data.csv"


def test_metadata_for_unknown_id_returns_empty_envelope() -> None:
    """Cache miss surfaces an empty :class:`SourceMetadata`.

    Sabotage proof: dropping the ``if message is None: return SourceMetadata()``
    guard would crash with an AttributeError.
    """
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    md = connector.metadata_for("never-seen")
    assert md.author is None
    assert md.tags == ()
    assert dict(md.properties) == {}


def test_metadata_for_falls_back_when_from_header_absent() -> None:
    """An envelope with no From header surfaces ``author=None`` cleanly.

    Sabotage proof: dropping the ``if from_addr`` guard before
    ``_extract_email_address`` would crash on None input.
    """
    msg = _make_message(
        "no-from",
        headers=(GmailHeader(name="Subject", value="Headerless"),),
    )
    client = _FakeGmailClient(
        messages=[msg],
        history_pages=[HistoryPage(message_ids=("no-from",), next_page_token=None, history_id="t")],
    )
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    list(connector.list_changes(cursor=None))
    list(connector.list_changes(cursor="cold-tip"))
    md = connector.metadata_for("no-from")
    assert md.author is None
    assert md.author_email is None


# ---------------------------------------------------------------------------
# Address-parsing helper branches driven through the public metadata_for
# (covers _split_addresses + _extract_email_address)
# ---------------------------------------------------------------------------


def _seeded_connector_with_headers(headers: tuple[GmailHeader, ...]) -> GmailConnector:
    """Build a connector pre-warmed with one message carrying the given headers."""
    msg = _make_message("addr-msg", headers=headers)
    client = _FakeGmailClient(
        messages=[msg],
        history_pages=[HistoryPage(message_ids=("addr-msg",), next_page_token=None, history_id="t")],
    )
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    list(connector.list_changes(cursor=None))
    list(connector.list_changes(cursor="cold-tip"))
    return connector


def test_metadata_for_handles_empty_to_header_as_no_tags() -> None:
    """An empty ``To`` value collapses to no tags (exercises empty-split branch).

    Sabotage proof: dropping the ``if not value`` guard in the address
    splitter would emit a single empty-string tag.
    """
    connector = _seeded_connector_with_headers((GmailHeader(name="To", value=""),))
    md = connector.metadata_for("addr-msg")
    assert md.tags == ()


def test_metadata_for_splits_comma_separated_to_recipients_and_strips_whitespace() -> None:
    """A comma-separated ``To`` value yields multiple tags (whitespace stripped).

    Sabotage proof: dropping the whitespace-strip / empty-filter logic
    would surface tags with leading/trailing spaces or empty entries.
    """
    connector = _seeded_connector_with_headers(
        (GmailHeader(name="To", value="alpha@example.com,  beta@example.com ,, gamma@example.com"),)
    )
    md = connector.metadata_for("addr-msg")
    assert md.tags == ("alpha@example.com", "beta@example.com", "gamma@example.com")


def test_metadata_for_extracts_address_from_angle_bracketed_from_header() -> None:
    """A ``Name <addr@example.com>`` From header surfaces ``author_email=addr``.

    Sabotage proof: removing the angle-bracket branch in the address
    extractor would surface the full "Name <addr>" string as author_email.
    """
    connector = _seeded_connector_with_headers(
        (GmailHeader(name="From", value="Agent Alpha <agent-alpha@example.com>"),)
    )
    md = connector.metadata_for("addr-msg")
    assert md.author == "Agent Alpha <agent-alpha@example.com>"
    assert md.author_email == "agent-alpha@example.com"


def test_metadata_for_extracts_bare_email_from_from_header() -> None:
    """A bare ``addr@host`` From header surfaces ``author_email=value``."""
    connector = _seeded_connector_with_headers((GmailHeader(name="From", value="agent-alpha@example.com"),))
    md = connector.metadata_for("addr-msg")
    assert md.author_email == "agent-alpha@example.com"


def test_metadata_for_returns_none_author_email_when_from_has_no_at() -> None:
    """A From header without any ``@`` returns ``author_email=None``."""
    connector = _seeded_connector_with_headers((GmailHeader(name="From", value="Agent Alpha (no email)"),))
    md = connector.metadata_for("addr-msg")
    assert md.author_email is None


def test_metadata_for_returns_none_author_email_for_unbalanced_brackets_without_at() -> None:
    """A From header with brackets but no @ inside returns ``author_email=None``."""
    connector = _seeded_connector_with_headers((GmailHeader(name="From", value="Agent <no-at-here>"),))
    md = connector.metadata_for("addr-msg")
    assert md.author_email is None


# ---------------------------------------------------------------------------
# Wave E per-container shim coverage (449-451, 473, 484-498, 516)
# ---------------------------------------------------------------------------


def _make_container(*, cursor_token: str | None = None) -> Container:
    return Container(
        cc_pair_id=1,
        container_id=_USER,
        access_state="ACCESSIBLE",
        cursor_token=cursor_token,
        last_synced_at=None,
    )


# NOTE: test_list_changes_for_container_flag_off_delegates_to_list_changes
# was retired alongside the topology_gmail flag (#132). Post-cutover the
# v2 per-mailbox cursor path is the only path; delegate-to-list_changes
# behaviour no longer exists. Parity tests covering the v2 path live below
# (test_list_changes_for_container_flag_on_* renamed in the same commit).


def test_list_changes_for_container_flag_on_cold_start_seeds_per_mailbox_cursor() -> None:
    """ON + cold-start (cursor None) seeds the per-mailbox cursor at the live tip.

    Sabotage proof: dropping the ``_next_cursor_by_container[mailbox] = history_id``
    assignment would leave next_cursor_for_container returning None even
    after a successful cold-start probe.
    """
    client = _FakeGmailClient(profile_history_id="live-tip-42")
    connector = GmailConnector(
        user_email=_USER,
        client=client,  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
        flag_reader=lambda _name: True,
    )
    events = list(connector.list_changes_for_container(_make_container(cursor_token=None)))
    assert events == []  # cold-start emits nothing
    assert connector.next_cursor_for_container(_USER) == "live-tip-42"


def test_list_changes_for_container_flag_on_warm_drain_threads_mailbox_metadata() -> None:
    """ON + warm cursor drains History API and stamps ``mailbox`` metadata onto events.

    Sabotage proof: dropping ``extra_metadata={"mailbox": mailbox}`` from
    the _list_changes_scoped call would lose per-mailbox attribution
    needed by downstream Silver routing.
    """
    msg = _make_message("scoped-msg")
    client = _FakeGmailClient(messages=[msg], terminal_history_id="post-drain-tip")
    connector = GmailConnector(
        user_email=_USER,
        client=client,  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
        flag_reader=lambda _name: True,
    )
    events = list(connector.list_changes_for_container(_make_container(cursor_token="warm-cursor-99")))
    assert len(events) == 1
    ev = events[0]
    assert ev.item_id == "scoped-msg"
    assert ev.metadata.get("mailbox") == _USER
    assert ev.metadata.get("sensitivity") == "client-confidential"
    assert connector.next_cursor_for_container(_USER) == "post-drain-tip"


def test_list_changes_for_container_flag_on_warm_drain_returns_none_when_no_terminal_tip() -> None:
    """ON branch — a drain with no advancing historyId returns None (don't-clobber).

    When the client surfaces no ``last_history_id`` the per-mailbox
    cursor must report None per the ``next_cursor`` Protocol contract:
    None means "no advance this tick; do not clobber the prior cursor".
    Echoing the stale input cursor would falsely signal an advance to a
    window we already drained, making the next tick re-query the
    identical window and re-emit every already-processed message.

    Sabotage proof: restoring the ``or cursor`` fallback in the cursor
    assignment makes this read the stale ``"warm-cursor-no-tip"`` and
    fail.
    """
    msg = _make_message("scoped-msg-no-tip")
    client = _FakeGmailClient(messages=[msg], terminal_history_id=None)
    connector = GmailConnector(
        user_email=_USER,
        client=client,  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
        flag_reader=lambda _name: True,
    )
    events = list(connector.list_changes_for_container(_make_container(cursor_token="warm-cursor-no-tip")))
    assert len(events) == 1, f"the message must still be emitted this tick; got {events!r}"
    # No advancing historyId → don't-clobber: report None, never the stale input cursor.
    assert connector.next_cursor_for_container(_USER) is None


def test_next_cursor_for_container_returns_none_for_unknown_mailbox() -> None:
    """Unknown mailbox id returns None (no synthetic value)."""
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    assert connector.next_cursor_for_container("never-seen@example.com") is None


def test_iter_containers_emits_single_container_with_cc_pair_id() -> None:
    """The mailbox surface emits exactly one Container row.

    Sabotage proof: yielding zero containers (e.g. via an early return)
    would surface as an empty list — the Wave E framework would skip
    every mailbox.
    """
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    containers = list(connector.iter_containers(cc_pair_id=99))
    assert len(containers) == 1
    assert containers[0].cc_pair_id == 99
    assert containers[0].container_id == _USER


def test_load_hierarchy_emits_single_root_folder() -> None:
    """Hierarchy surface emits exactly one root FOLDER for the mailbox."""
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    nodes = list(connector.load_hierarchy(cc_pair_id=7))
    assert len(nodes) == 1
    n = nodes[0]
    assert n.node_type == "FOLDER"
    assert n.raw_parent_id is None
    assert n.cc_pair_id == 7
    assert _USER in n.display_name


def test_load_hierarchy_root_link_keeps_path_separator_before_fragment() -> None:
    """The root FOLDER links to the exact Gmail inbox URL.

    Regression (PLA-472): the link was built with ``str.rstrip('#inbox/')``,
    which strips a character SET rather than a suffix, so the ``/`` before
    the ``#inbox`` fragment was eaten (``.../u/0#inbox``).
    """
    client = _FakeGmailClient()
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    (node,) = list(connector.load_hierarchy(cc_pair_id=7))
    assert node.link == "https://mail.google.com/mail/u/0/#inbox"


def test_load_from_checkpoint_delegates_to_list_changes() -> None:
    """The Checkpointed shim threads the checkpoint string into list_changes.

    Sabotage proof: changing load_from_checkpoint to ignore the
    checkpoint and always pass None would mean every warm tick
    re-cold-starts and emits zero events.
    """
    msg = _make_message("ck-msg")
    client = _FakeGmailClient(
        messages=[msg],
        history_pages=[HistoryPage(message_ids=("ck-msg",), next_page_token=None, history_id="t")],
    )
    connector = GmailConnector(user_email=_USER, client=client)  # type: ignore[arg-type]  # F3 rationale: test-local stub mirrors GmailClient shape.
    events = list(connector.load_from_checkpoint(_make_container(cursor_token="warm"), "ck-token"))
    assert events
    assert events[0].item_id == "ck-msg"


# ---------------------------------------------------------------------------
# Default client builder threaded via constructor (line 200)
# ---------------------------------------------------------------------------


def test_gmail_connector_constructed_with_credentials_uses_default_client_builder() -> None:
    """Constructing with ``credentials=`` (and no ``client=``) wires the default builder.

    The default builder constructs a real :class:`GmailClient` carrying
    the OAuth refresher closure. We confirm the connector exposes a
    Gmail-style ``next_cursor()`` surface and accepts the mailbox routing.

    Sabotage proof: removing the ``client_builder(resolved_credentials, user_email)``
    branch in :meth:`GmailConnector.__init__` would crash on first
    list_changes with an AttributeError on ``self._client``.
    """
    creds = GmailCredentials(
        client_id="cid",
        client_secret="csec",  # pragma: allowlist secret
        refresh_token="rt",  # pragma: allowlist secret
        access_token="live-bearer",  # pragma: allowlist secret
    )
    # No ``client=`` arg — the production default_client_builder fires.
    connector = GmailConnector(user_email=_USER, credentials=creds)
    # Construction does no I/O; surface the next_cursor accessor to confirm wiring.
    assert connector.next_cursor() is None
    assert connector.name == "gmail"


# ---------------------------------------------------------------------------
# make_connector — config validation (588-613)
# ---------------------------------------------------------------------------


def test_make_connector_missing_user_email_raises_with_fix_pointer() -> None:
    """Missing ``user_email`` config key yields an actionable ValueError.

    Sabotage proof: dropping the isinstance/empty guard would let the
    factory silently construct a connector with no mailbox routing.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({})
    msg = str(exc_info.value)
    assert "user_email" in msg
    assert "fix:" in msg
    assert "next:" in msg


def test_make_connector_non_string_user_email_raises_with_fix_pointer() -> None:
    """A non-string user_email yields the same actionable ValueError shape."""
    with pytest.raises(ValueError):
        make_connector({"user_email": 12345})


def test_make_connector_empty_string_user_email_raises_with_fix_pointer() -> None:
    """An empty-string user_email yields ValueError."""
    with pytest.raises(ValueError):
        make_connector({"user_email": ""})


def test_make_connector_invalid_sensitivity_raises_with_valid_tier_list() -> None:
    """An invalid sensitivity tier surfaces the valid-tier list in the error.

    Sabotage proof: dropping the ``in _VALID_SENSITIVITY_TIERS`` check
    would let any string slip through and reach the Sensitivity literal
    silently — type-checked at runtime by F39 downstream.
    """
    with pytest.raises(ValueError) as exc_info:
        make_connector({"user_email": _USER, "sensitivity": "ultra-secret"})
    msg = str(exc_info.value)
    assert "ultra-secret" in msg
    assert "fix:" in msg
    assert "next:" in msg


def test_make_connector_non_string_sensitivity_raises() -> None:
    """A non-string sensitivity (e.g. int) yields the same ValueError shape."""
    with pytest.raises(ValueError):
        make_connector({"user_email": _USER, "sensitivity": 7})


def test_make_connector_happy_path_resolves_secrets_via_xdg_secret_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete config + secrets directory yields a constructed connector.

    Drives the production secret-resolution path via an XDG-redirected
    secret directory (non-KAIRIX env var, no patching of kairix internals).
    Sabotage proof: removing the final ``return GmailConnector(...)`` in
    make_connector would surface as None on the return value.
    """
    secrets_dir = tmp_path / "kairix" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "connector-gmail-client-id").write_text("cid", encoding="utf-8")
    (secrets_dir / "connector-gmail-client-secret").write_text("csec", encoding="utf-8")
    (secrets_dir / "connector-gmail-refresh-token").write_text("rt", encoding="utf-8")
    # XDG_CONFIG_HOME is a non-KAIRIX env var redirecting the XDG secret path;
    # F2 only blocks KAIRIX_* setenv. The redirect is the canonical pattern for
    # exercising the file-based secret backend without patching kairix internals.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # KAIRIX_SECRETS_DIR is blocked by F2 (KAIRIX_*); use XDG_CONFIG_HOME redirect instead.
    connector = make_connector({"user_email": _USER, "sensitivity": "internal"})
    assert connector.name == "gmail"
    assert connector.sensitivity_for("anything") == "internal"


def test_make_connector_defaults_sensitivity_to_client_confidential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a sensitivity override, default is ``client-confidential``."""
    secrets_dir = tmp_path / "kairix" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "connector-gmail-client-id").write_text("cid", encoding="utf-8")
    (secrets_dir / "connector-gmail-client-secret").write_text("csec", encoding="utf-8")
    (secrets_dir / "connector-gmail-refresh-token").write_text("rt", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    connector = make_connector({"user_email": _USER})
    assert connector.sensitivity_for("anything") == "client-confidential"
