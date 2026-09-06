"""``M365EmailHeadersConnector`` — SourceConnector for M365 header-only sync.

Implements :class:`kairix.core.protocols.SourceConnector` for a single
M365 mailbox via Microsoft Graph. Per ADR-004 (Email — Headers Only),
the connector **never** fetches body content; the
:func:`kairix.connectors.m365_email_headers.graph_client.HEADER_ONLY_SELECT`
projection is the mechanical guard at the Graph query layer.

Folder-scoped delta (#380): Graph rejects mailbox-wide
``/users/{upn}/messages/delta`` with ``BadRequest: Change tracking is
not supported against 'microsoft.graph.message'``. Delta only works
folder-scoped, so the connector enumerates mail folders via
:meth:`M365GraphClient.list_mail_folders` and drains each folder's
delta independently. Each folder owns its own deltaLink — the
connector's serialised cursor is a JSON-encoded
``{folder_id: deltaLink}`` mapping. One bad folder (transient 5xx,
throttle exhaustion) is logged + skipped + retried on the next tick;
sibling folders make progress regardless.

Cursor model:

  * First sync (``cursor is None``) — call
    :meth:`M365GraphClient.list_mail_folders` to enumerate folders,
    then call :meth:`M365GraphClient.iter_messages` per folder from
    the seed delta URL, yield one ``created`` :class:`ChangeEvent`
    per message, then persist the merged ``{folder_id: deltaLink}``
    mapping as the cursor.

  * Subsequent ticks — decode the cursor as a JSON mapping, pass
    each folder's deltaLink back into
    :meth:`M365GraphClient.iter_messages`; the Graph endpoint returns
    only items changed since the cursor.

  * Legacy / unrecognised cursor — any cursor that isn't a JSON dict
    is treated as cold-start (so old single-string cursors stored
    before the #380 fix trigger a fresh per-folder full sync rather
    than a crash).

Folder allowlist: operators can restrict which folders sync via the
``folders_allowlist: ["inbox", "sentitems", "archive"]`` config key.
Well-known folder names (``inbox``, ``sentitems``, ``drafts``,
``deleteditems``, ``junkemail``, ``outbox``, ``archive``) are matched
case-insensitively against the Graph ``wellKnownName`` field; custom
folders are matched case-insensitively against ``displayName``. An
empty / missing allowlist ingests every folder.

``fetch`` returns a small JSON artefact containing only header fields
— the orchestration layer routes this through the canonical Silver
processor which extracts entity signals (people from
from/to/cc, subject as a timeline-update token) WITHOUT producing
chunks that contain body text (because there is no body text).

Per F35, this module only imports from ``kairix.connectors.m365_email_headers.*``
(same plugin), ``kairix.core.*`` (the Protocol surface), and
``kairix.transport.auth.*`` (the shared OAuth2 helper). No reach into
other connectors, no reach into the extractor layer.

Per F44, no Postgres / asyncpg / psycopg imports anywhere in this tree
— state lives in the connector_cursors SQLite table managed by
``kairix.core.connectors.cursor_store``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from kairix.connectors.m365_email_headers.graph_client import (
    GraphMessage,
    M365GraphClient,
    MailFolderRef,
)
from kairix.core.protocols import (
    ChangeEvent,
    Container,
    Cursor,
    HierarchyNode,
    RawArtefact,
    Sensitivity,
    SourceMetadata,
)
from kairix.secrets.loader import SecretsLoader, SecretsResolver
from kairix.transport.auth.oauth2_client_creds import (
    OAuth2ClientCredsAuth,
)
from kairix.transport.errors import GraphDeltaExpiredError

logger = logging.getLogger(__name__)

CONNECTOR_NAME = "m365_email_headers"

# Per ADR-004 + ADR-005: email headers are personal-tier data. The
# tier is locked at the connector boundary — operators cannot lower
# the sensitivity via config because that would let a misconfigured
# deploy index personal data as public.
LOCKED_SENSITIVITY: Sensitivity = "personal"

# Microsoft Graph client-credentials scope for app-only mailbox reads.
# Always ``.default`` per the Microsoft v2 endpoint convention — the
# resolved permissions come from the AAD app registration's API
# permissions, not from this string.
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Mime hint for the fetched header-only artefact. Header envelopes are
# stored as JSON so the downstream Silver processor (entity signal
# extraction) reads structured fields directly rather than re-parsing
# RFC822.
HEADER_ARTEFACT_MIME = "application/json"

# Stable identifier for the synthetic root hierarchy node. Each mailbox
# FOLDER node hangs off this single root so the receiver builds the
# tree in one pass (F58 parent-before-child).
_HIERARCHY_ROOT_ID = "m365-email-headers"

# F17 — extract the ``"sensitivity"`` metadata key (used in legacy +
# Wave E ChangeEvent emission + make_connector config validation) so
# the literal lives in one place.
_SENSITIVITY_METADATA_KEY = "sensitivity"

# F17 — ChangeEvent metadata key for the source folder so downstream
# consumers (Silver entity extraction, retrieval scoping) can scope by
# folder without re-hitting Graph.
_FOLDER_METADATA_KEY = "folder"

# Microsoft Graph documented well-known folder names per
# https://learn.microsoft.com/graph/api/resources/mailfolder — used by
# the optional ``folders_allowlist`` filter to match folders without
# the operator needing the server-assigned folder id.
_WELL_KNOWN_FOLDER_NAMES: frozenset[str] = frozenset(
    {"inbox", "sentitems", "drafts", "deleteditems", "junkemail", "outbox", "archive"}
)


def _now_iso() -> str:
    """Return a current ISO-8601 UTC timestamp matching the connector
    boundary's :class:`ChangeEvent.modified_at` format.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class M365Credentials:
    """Resolved client-credentials triple for one M365 sync.

    Frozen per F42 — the dataclass is the typed shape that crosses the
    boundary between secret resolution and the connector constructor.
    Tests construct a literal :class:`M365Credentials` and pass it via
    the ``credentials`` kwarg; production resolves through
    :func:`_resolve_credentials_from_secrets`.
    """

    tenant_id: str
    client_id: str
    client_secret: str


def _default_flag_reader(name: str) -> bool:
    """Production default for the topology-m365_email_headers flag check.

    Delegates to :func:`kairix.core.features.flag` so the production
    path threads through the env-var → config-overlay → registry
    resolution chain. Tests inject a different callable (typically one
    backed by :class:`tests.fakes.FakeFeatureFlagResolver`) so the
    branch under test is pinned without monkey-patching the resolver
    module (F1-clean / F2-clean).

    Lifted to a module-level helper so the connector's signature can
    carry a real callable default (F6-clean) without a per-call
    ``Optional[...] = None`` shape.
    """
    from kairix.core.features import flag as _prod_flag

    return _prod_flag(name)


def _default_client_builder(auth: OAuth2ClientCredsAuth, upn: str) -> M365GraphClient:
    """Production default for constructing a per-mailbox Graph client.

    Lifted to module-level so the connector's constructor can carry a
    real callable default (F6-clean) — tests pass a stub builder that
    returns an :class:`httpx.MockTransport`-backed client.
    """
    return M365GraphClient(user_principal_name=upn, auth=auth)


def _resolve_credentials_from_secrets(secrets: SecretsResolver) -> M365Credentials:
    """Resolve the three required secrets via the canonical :class:`SecretsResolver`.

    Each call uses :meth:`SecretsResolver.require` so a missing secret
    raises :class:`kairix.secrets.SecretNotFoundError` with the canonical
    KV name + env-var in the message. Per ADR-031, the canonical identity
    tuple is ``(connector, m365, None, <leaf>)``; the loader's legacy-
    alias fallback resolves the historical ``CONNECTOR_M365_*`` env vars
    transparently so existing deployments keep working unchanged.
    """
    tenant = secrets.require("connector", "m365", None, "tenant-id")
    client = secrets.require("connector", "m365", None, "client-id")
    secret = secrets.require("connector", "m365", None, "client-secret")
    return M365Credentials(tenant_id=tenant, client_id=client, client_secret=secret)


class M365EmailHeadersConnector:
    """SourceConnector for a single M365 mailbox, header-only.

    Construction acquires the OAuth2 client-creds shape via the
    injected ``credentials`` (tests pass a literal; production resolves
    from :mod:`kairix.secrets`). The first :meth:`list_changes` call
    drives the Graph delta query from the seed URL; subsequent calls
    resume from the previous tick's deltaLink (the ``cursor`` string).

    DI seams:

      * ``credentials`` — resolved :class:`M365Credentials`. Tests pass
        a literal; production callers omit and the factory resolves
        from :mod:`kairix.secrets`.
      * ``graph_client_factory`` — builds the
        :class:`M365GraphClient`. Tests pass a factory returning a
        client backed by an ``httpx.MockTransport`` so no real Graph
        call leaks.
    """

    name: str = CONNECTOR_NAME
    per_tick_max_items: int = 500
    # F66-watermark-exempt: headers-only fetch; no message bodies persisted to disk
    disk_watermark_min_free_bytes: int | None = None

    def __init__(
        self,
        user_principal_name: str,
        *,
        credentials: M365Credentials | None = None,
        client_builder: Callable[[OAuth2ClientCredsAuth, str], M365GraphClient] | None = None,
        auth: OAuth2ClientCredsAuth | None = None,
        mailboxes: Sequence[str] | None = None,
        folders_allowlist: Sequence[str] | None = None,
        flag_reader: Callable[[str], bool] = _default_flag_reader,
        secrets: SecretsResolver | None = None,
    ) -> None:
        if not user_principal_name:
            raise ValueError(
                "m365_email_headers: user_principal_name is empty. "
                "fix: set user_principal_name in the connector config block. "
                "next: see docs/architecture/connector-ingestion-architecture.md §8."
            )
        self._upn = user_principal_name

        # Wave E topology — each configured mailbox is a Container with
        # its own delta cursor. The primary ``user_principal_name`` is
        # always included so the legacy single-mailbox config keeps
        # working without an explicit ``mailboxes`` block. Sorted for
        # deterministic ``iter_containers`` / ``load_hierarchy`` order
        # (mirrors the obsidian Wave E pilot's sort behaviour).
        all_mailboxes: list[str] = [user_principal_name]
        if mailboxes is not None:
            for extra in mailboxes:
                if extra and extra not in all_mailboxes:
                    all_mailboxes.append(extra)
        self._mailboxes: tuple[str, ...] = tuple(sorted(all_mailboxes))

        self._secrets: SecretsResolver = secrets if secrets is not None else SecretsLoader()
        resolved_auth: OAuth2ClientCredsAuth
        if auth is not None:
            resolved_auth = auth
        else:
            creds = credentials if credentials is not None else _resolve_credentials_from_secrets(self._secrets)
            resolved_auth = OAuth2ClientCredsAuth(
                tenant_id=creds.tenant_id,
                client_id=creds.client_id,
                client_secret=creds.client_secret,
                scope=GRAPH_DEFAULT_SCOPE,
            )
        self._auth = resolved_auth
        # Hold the client_builder so per-mailbox Graph clients can be
        # lazily constructed for the Wave E ON branch
        # (``list_changes_for_container``). Each mailbox needs its own
        # client because the UPN is baked into the Graph URL template;
        # caching keeps per-tick construction cheap.
        self._client_builder: Callable[[OAuth2ClientCredsAuth, str], M365GraphClient] = (
            client_builder if client_builder is not None else _default_client_builder
        )
        self._flag_reader = flag_reader

        # The legacy single-mailbox Graph client — used by ``list_changes``
        # for the OFF branch and by ``fetch`` after either branch has
        # primed the per-tick cache.
        self._graph = self._client_builder(resolved_auth, user_principal_name)
        # Per-mailbox Graph clients keyed by UPN. Populated lazily on the
        # first Wave E ``list_changes_for_container`` call for each
        # mailbox so the OFF branch pays nothing.
        self._per_mailbox_graph: dict[str, M365GraphClient] = {user_principal_name: self._graph}
        # Cache for last-fetched messages so ``fetch`` can return the
        # already-acquired header envelope without a second Graph call.
        # Bronze-write happens once per item per tick.
        self._cache: dict[str, GraphMessage] = {}
        # The next-tick cursor — populated after a successful
        # ``list_changes`` drain.
        self._next_cursor: str | None = None
        # Per-container next-cursors — populated by
        # ``list_changes_for_container`` so the framework can persist a
        # distinct deltaLink per mailbox to ``topology_containers.cursor_token``.
        self._next_cursor_by_container: dict[str, str | None] = {}
        # Per-folder allowlist (#380). ``None`` / empty = ingest every
        # folder; a populated tuple restricts to folders matched by
        # well-known name (case-insensitive) or display name
        # (case-insensitive).
        self._folders_allowlist: tuple[str, ...] | None = tuple(folders_allowlist) if folders_allowlist else None

    # ------------------------------------------------------------------
    # SourceConnector Protocol surface
    # ------------------------------------------------------------------

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream header-only changes per folder from Graph since ``cursor``.

        Folder-scoped delta (#380): Graph rejects mailbox-wide delta, so
        the connector enumerates mail folders via
        :meth:`M365GraphClient.list_mail_folders` (optionally filtered
        by the ``folders_allowlist`` config), then drains each folder's
        delta independently. ``cursor`` decodes as a JSON
        ``{folder_id: deltaLink}`` mapping carrying each folder's
        previous-tick deltaLink; ``None`` (or any non-JSON / non-dict
        legacy cursor) triggers a cold-start full sync per folder.

        Each ``GraphMessage`` becomes one ``created`` :class:`ChangeEvent`;
        ``metadata`` carries the source folder under the
        :data:`_FOLDER_METADATA_KEY` key so downstream consumers can
        scope by folder without re-hitting Graph. Graph itself handles
        the modified / deleted distinction in future syncs.

        One bad folder (transient ``httpx.HTTPError``) is logged + skipped
        + retried on the next tick. Sibling folders make progress
        regardless: their next-cursor is still recorded and their events
        are still yielded.
        """
        previous_cursors = _decode_per_folder_cursor(cursor)
        events, next_cursors = self._drain_all_folders(
            graph=self._graph,
            previous_cursors=previous_cursors,
            extra_metadata={},
        )
        self._next_cursor = _encode_per_folder_cursor(next_cursors)
        return iter(events)

    def _drain_all_folders(
        self,
        *,
        graph: M365GraphClient,
        previous_cursors: dict[str, str],
        extra_metadata: dict[str, str],
    ) -> tuple[list[ChangeEvent], dict[str, str]]:
        """Enumerate folders, drain each, return events + next-cursors mapping.

        Shared between :meth:`list_changes` (legacy / OFF-branch) and
        :meth:`_list_changes_scoped` (Wave E ON-branch) so the
        folder-enumerate → per-folder-drain → cursor-merge loop lives
        in one place. The caller passes the per-mailbox Graph client
        plus any extra ChangeEvent metadata (e.g. the Wave E
        ``mailbox`` key) it wants threaded onto every event.

        Returns ``([], previous_cursors)`` when the folder enumeration
        call itself fails so the caller can persist the unchanged
        cursor mapping and the next tick can resume.
        """
        events: list[ChangeEvent] = []
        try:
            folders = graph.list_mail_folders()
        except httpx.HTTPError as exc:
            logger.warning("m365 graph: list_mail_folders failed; deferring to next tick: %s", exc)
            return events, dict(previous_cursors)

        next_cursors: dict[str, str] = {}
        for folder in _select_folders(folders, self._folders_allowlist):
            self._drain_one_folder(
                graph=graph,
                folder=folder,
                previous_cursors=previous_cursors,
                next_cursors=next_cursors,
                events=events,
                extra_metadata=extra_metadata,
            )
        return events, next_cursors

    def _drain_one_folder(
        self,
        *,
        graph: M365GraphClient,
        folder: MailFolderRef,
        previous_cursors: dict[str, str],
        next_cursors: dict[str, str],
        events: list[ChangeEvent],
        extra_metadata: dict[str, str],
    ) -> None:
        """Drain one folder's delta; record cursor; skip on HTTPError.

        Per #380's per-folder isolation contract: an ``httpx.HTTPError``
        on this folder's drain is logged and the folder's prior cursor
        (if any) is preserved so the next tick retries from the same
        horizon. Siblings make progress regardless.
        """
        start_url = previous_cursors.get(folder.folder_id)
        try:
            folder_events = self._collect_folder_events(
                graph=graph,
                folder=folder,
                start_url=start_url,
                extra_metadata=extra_metadata,
            )
        except GraphDeltaExpiredError:
            if start_url is None:
                self._preserve_folder_cursor(folder, previous_cursors, next_cursors, "initial seed returned 410")
                return
            logger.warning(
                "m365 graph: folder %r delta cursor expired; restarting that folder from its initial seed",
                folder.display_name or folder.folder_id,
            )
            try:
                folder_events = self._collect_folder_events(
                    graph=graph,
                    folder=folder,
                    start_url=None,
                    extra_metadata=extra_metadata,
                )
            except httpx.HTTPError as exc:
                self._preserve_folder_cursor(folder, previous_cursors, next_cursors, str(exc))
                return
        except httpx.HTTPError as exc:
            self._preserve_folder_cursor(folder, previous_cursors, next_cursors, str(exc))
            return
        events.extend(folder_events)
        terminal = graph.last_delta_link()
        if isinstance(terminal, str) and terminal:
            next_cursors[folder.folder_id] = terminal
        elif start_url is not None:
            next_cursors[folder.folder_id] = start_url

    def _collect_folder_events(
        self,
        *,
        graph: M365GraphClient,
        folder: MailFolderRef,
        start_url: str | None,
        extra_metadata: dict[str, str],
    ) -> list[ChangeEvent]:
        """Stage a complete folder drain so a failed page emits no partial batch."""
        messages = list(graph.iter_messages(folder.folder_id, start_url=start_url))
        folder_events: list[ChangeEvent] = []
        for message in messages:
            self._cache[message.message_id] = message
            metadata: dict[str, str] = {
                _SENSITIVITY_METADATA_KEY: LOCKED_SENSITIVITY,
                _FOLDER_METADATA_KEY: folder.display_name or folder.folder_id,
            }
            metadata.update(extra_metadata)
            folder_events.append(
                ChangeEvent(
                    op="created",
                    item_id=message.message_id,
                    modified_at=_event_modified_at(message),
                    metadata=metadata,
                )
            )
        return folder_events

    @staticmethod
    def _preserve_folder_cursor(
        folder: MailFolderRef,
        previous_cursors: dict[str, str],
        next_cursors: dict[str, str],
        detail: str,
    ) -> None:
        """Keep one failed folder's prior horizon without stopping siblings."""
        logger.warning(
            "m365 graph: folder %r drain failed; keeping previous cursor and skipping for this tick: %s",
            folder.display_name or folder.folder_id,
            detail,
        )
        prior = previous_cursors.get(folder.folder_id)
        if prior is not None:
            next_cursors[folder.folder_id] = prior

    def fetch(self, item_id: str) -> RawArtefact:
        """Return the cached header envelope for ``item_id`` as JSON.

        ``list_changes`` populates the cache; ``fetch`` reads it. The
        artefact is a JSON serialisation of the header-only fields —
        body content is never present because the Graph projection
        never asked for it.
        """
        message = self._cache.get(item_id)
        if message is None:
            raise KeyError(
                f"m365_email_headers: item_id {item_id!r} not in the per-tick cache. "
                "fix: call list_changes() before fetch() so the Graph delta drains "
                "the envelope before the orchestrator asks for the body. "
                "next: see kairix/core/connectors/pipeline.py for the orchestrator's "
                "list_changes -> fetch contract."
            )
        payload = json.dumps(
            {
                "id": message.message_id,
                "from": message.sender,
                "toRecipients": list(message.to_recipients),
                "ccRecipients": list(message.cc_recipients),
                "subject": message.subject,
                "sentDateTime": message.sent_at,
                "receivedDateTime": message.received_at,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return RawArtefact(
            raw=payload,
            mime=HEADER_ARTEFACT_MIME,
            fetched_at=_now_iso(),
        )

    def source_link(self, item_id: str) -> str:
        """Return the Outlook on the Web deep link for the message.

        Outlook accepts the Graph message id directly in the
        ``ItemID`` query parameter; the URL round-trips operators
        from a retrieval result back into the original message in
        their inbox.
        """
        return f"https://outlook.office.com/mail/inbox/id/{quote(item_id, safe='')}"

    def sensitivity_for(self, _item_id: str) -> Sensitivity:
        """Always return the locked ``personal`` tier per ADR-004 + ADR-005.

        v1 has no per-item overrides — every message envelope from
        the connector carries the personal tier. A future ADR can add
        per-message classification (e.g. via Microsoft Information
        Protection labels) without breaking the Protocol.
        """
        return LOCKED_SENSITIVITY

    # ------------------------------------------------------------------
    # Topology Wave B — capability mix-in shims (no behavioural change)
    # ------------------------------------------------------------------
    # The shims below let the connector satisfy the new capability
    # Protocols (CheckpointedConnector, CredentialsConnector,
    # OAuthConnector) by delegating to existing methods OR raising
    # actionable NotImplementedError where the source kind does not
    # support the surface. Production routing through these methods is
    # gated by ``topology_protocol`` (default-off).

    def load_from_checkpoint(self, _container: Container, checkpoint: str | None) -> Iterator[ChangeEvent]:
        """CheckpointedConnector shim — delegate to :meth:`list_changes` using the checkpoint.

        Graph delta works on opaque deltaLink strings; the shim forwards
        ``checkpoint`` (or ``None`` for cold-start) directly to
        :meth:`list_changes` so observable behaviour matches the v1 path.
        ``_container`` is accepted for Protocol compliance but the
        legacy path is single-mailbox per cc_pair (Wave E activates
        per-container routing).
        """
        return self.list_changes(checkpoint)

    # ------------------------------------------------------------------
    # Topology Wave E — per-mailbox multi-container pilot
    # ------------------------------------------------------------------
    # Wave B landed shim implementations of the capability Protocols
    # (PollConnector / CheckpointedConnector / HierarchyConnector). Wave E
    # adds real implementations behind the
    # ``topology_m365_email_headers`` flag:
    #
    #   * :meth:`iter_containers` — one :class:`Container` per configured
    #     mailbox UPN, each with its own per-mailbox Graph delta cursor.
    #   * :meth:`list_changes_for_container` — when flag ON, drives a
    #     :meth:`M365GraphClient.iter_messages` call against
    #     ``container.container_id`` ONLY using ``container.cursor_token``
    #     as the per-mailbox deltaLink. When flag OFF, retains the Wave B
    #     shim shape (delegate to :meth:`list_changes`).
    #   * :meth:`load_hierarchy` — when flag ON, emits one root FOLDER
    #     node + one FOLDER per configured mailbox (parent-before-child
    #     per F58). When flag OFF, retains the Wave B shim shape (one
    #     root FOLDER node only).
    #
    # The flag defaults OFF so existing operators see bit-for-bit
    # current behaviour. The ON branch is the per-mailbox pattern that
    # unlocks independent per-mailbox sync cadence and isolated cursor
    # state per ADR v2 §Wave E.

    def iter_containers(self, cc_pair_id: int) -> Iterator[Container]:
        """Yield one :class:`Container` per configured mailbox.

        Topology §4: each Container has its own delta cursor — the
        Wave E pilot maps each configured mailbox UPN to its own
        Container so the operator can sync different mailboxes at
        different cadences and scope retrieval per-mailbox via the
        topology collection mapping.

        Calling convention: the framework's lifecycle layer (see
        ``kairix/core/connectors/cc_pair.py``) passes ``cc_pair_id`` so
        the connector can construct the Container without reaching back
        into the cc_pair store. Mirrors the dispatch shape the
        ``HierarchyConnector.load_hierarchy(cc_pair_id)`` Protocol
        method already uses.

        ``access_state`` is always ``ACCESSIBLE`` — Graph permission
        checks happen downstream at the request layer (a permission-
        denied response surfaces as a typed error to the framework,
        which flips the Container's state to ``REVOKED`` via the
        topology access lifecycle). ``cursor_token`` and
        ``last_synced_at`` start ``None``; the framework persists
        subsequent values to the ``topology_containers`` table.
        """
        for mailbox in self._mailboxes:
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id=mailbox,
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )

    def list_changes_for_container(self, container: Container) -> Iterator[ChangeEvent]:
        """Stream changes for one mailbox Container.

        Drives a Graph ``/users/{container.container_id}/messages/delta``
        query starting from ``container.cursor_token`` (the previous
        tick's deltaLink for that mailbox). Emits ChangeEvents only for
        messages in that mailbox; per-container deltaLink is recorded
        via :meth:`next_cursor_for_container` so the framework can
        persist it independently from sibling containers.

        ``topology_m365_email_headers`` retired post-cutover
        (task #132); the per-mailbox path is now the only behaviour.
        """
        return self._list_changes_scoped(container)

    def retrieve_all_slim_docs(self, _container: Container) -> Iterator[str]:
        """SlimConnector shim — Graph delta is ID-only friendly via iter_messages.

        Folder-scoped delta (#380): drains each enumerated folder via
        the per-mailbox Graph client and yields one ``message_id``
        per envelope for the prune cycle's diff against
        ``documents.item_id``. The container's ``cursor_token`` is the
        JSON ``{folder_id: deltaLink}`` mapping (same shape as the
        legacy :meth:`list_changes` cursor); each folder resumes from
        its own deltaLink.
        """
        mailbox = _container.container_id or self._upn
        graph = self._per_mailbox_client(mailbox)
        try:
            folders = graph.list_mail_folders()
        except httpx.HTTPError as exc:
            logger.warning("m365 graph: slim-doc folder enumeration failed; yielding empty set: %s", exc)
            return
        previous_cursors = _decode_per_folder_cursor(_container.cursor_token)
        selected = _select_folders(folders, self._folders_allowlist)
        for folder in selected:
            start_url = previous_cursors.get(folder.folder_id)
            try:
                for message in graph.iter_messages(folder.folder_id, start_url=start_url):
                    yield message.message_id
            except httpx.HTTPError as exc:
                logger.warning(
                    "m365 graph: slim-doc folder %r drain failed; skipping for this tick: %s",
                    folder.display_name or folder.folder_id,
                    exc,
                )
                continue

    def load_hierarchy(self, cc_pair_id: int) -> Iterator[HierarchyNode]:
        """HierarchyConnector — emit one root FOLDER + one FOLDER per mailbox.

        Emits a synthetic root FOLDER node (``raw_node_id="m365-email-headers"``,
        ``raw_parent_id=None``) followed by one FOLDER per configured
        mailbox as children of root. Order is root-first then mailboxes
        in sorted UPN order so parent-before-child per F58 holds
        trivially in a single pass.

        ``raw_node_id`` for the per-mailbox FOLDER is the mailbox UPN
        itself (e.g. ``alice@contoso.com``) so the topology hierarchy
        store can round-trip without further mapping. ``link`` is an
        Outlook on the Web inbox URL for that mailbox so the search
        layer can surface a clickable affordance. ``sensitivity_hint``
        is ``"personal"`` — the connector's locked tier per ADR-004 +
        ADR-005.

        Inbox / Sent / other Graph mail folders are NOT walked at this
        slice as separate hierarchy nodes — the per-folder delta loop
        (#380) drains each Graph mail folder for messages, but the
        hierarchy emits one synthetic FOLDER per mailbox (not per Graph
        mail folder).

        ``topology_m365_email_headers`` retired post-cutover
        (task #132); the root + per-mailbox emission is now the only
        behaviour.
        """
        # Root node first — F58 parent-before-child invariant.
        yield HierarchyNode(
            cc_pair_id=cc_pair_id,
            raw_node_id=_HIERARCHY_ROOT_ID,
            raw_parent_id=None,
            display_name="M365 Email (Headers)",
            link=None,
            node_type="FOLDER",
            external_access_json=None,
            sensitivity_hint=None,
        )
        # ``sensitivity_hint`` uses the F39 tier vocabulary
        # (public/internal/confidential/restricted) which doesn't include
        # the legacy ``personal`` literal. Map the connector's locked
        # personal tier onto ``restricted`` — the F39 tier that conveys
        # "tightest engagement-scope access" — so the hint surfaces
        # something meaningful at the hierarchy boundary. The connector
        # boundary continues to tag chunks with the legacy ``personal``
        # tier via ``sensitivity_for`` per ADR-004 + ADR-005.
        for mailbox in self._mailboxes:
            yield HierarchyNode(
                cc_pair_id=cc_pair_id,
                raw_node_id=mailbox,
                raw_parent_id=_HIERARCHY_ROOT_ID,
                display_name=mailbox,
                link=f"https://outlook.office.com/mail/{quote(mailbox, safe='@')}/inbox",
                node_type="FOLDER",
                external_access_json=None,
                sensitivity_hint="restricted",
            )

    def next_cursor_for_container(self, container_id: str) -> str | None:
        """Return the deltaLink the framework should persist for one mailbox.

        Populated by :meth:`list_changes_for_container` on the Wave E ON
        branch; ``None`` if that mailbox has not yet been drained this
        process lifetime or if Graph returned no terminal deltaLink.

        Distinct from :meth:`next_cursor` because Wave E persists one
        cursor per :class:`Container` (per mailbox) rather than a single
        connector-wide cursor.
        """
        return self._next_cursor_by_container.get(container_id)

    def _list_changes_scoped(self, container: Container) -> Iterator[ChangeEvent]:
        """Wave E ON-branch: drain folder-scoped delta against one mailbox.

        Reads ``container.cursor_token`` as a JSON
        ``{folder_id: deltaLink}`` mapping (per-mailbox state of every
        folder's cursor); ``None`` (or any non-JSON legacy cursor)
        triggers a cold-start full sync per folder. Drives a Graph
        ``/users/{container.container_id}/mailFolders/{folder_id}/messages/delta``
        iteration per folder via the per-mailbox Graph client, emits one
        ``created`` ChangeEvent per envelope, primes the per-tick fetch
        cache, and records the merged per-folder cursor mapping in
        ``_next_cursor_by_container`` so the framework can persist a
        distinct (per-mailbox, per-folder) cursor.

        Per-mailbox isolation is structural: each mailbox owns its
        Graph client (UPN baked in), its cursor read (the container's
        ``cursor_token`` only), and its next-cursor write (keyed by
        ``container.container_id`` in ``_next_cursor_by_container``).
        One bad folder doesn't poison the others — the same skip-and-
        retry-next-tick policy from :meth:`list_changes` applies here
        (both methods share :meth:`_drain_all_folders`).
        """
        mailbox = container.container_id
        graph = self._per_mailbox_client(mailbox)
        previous_cursors = _decode_per_folder_cursor(container.cursor_token)
        events, next_cursors = self._drain_all_folders(
            graph=graph,
            previous_cursors=previous_cursors,
            extra_metadata={"mailbox": mailbox},
        )
        self._next_cursor_by_container[mailbox] = _encode_per_folder_cursor(next_cursors)
        return iter(events)

    def _per_mailbox_client(self, mailbox: str) -> M365GraphClient:
        """Resolve (or lazily build) the Graph client for one mailbox.

        The primary ``user_principal_name`` mailbox's client is created
        in ``__init__`` so the OFF branch pays the same construction
        cost it did pre-Wave-E. Additional mailboxes get their client
        built on first ON-branch access via the injected
        ``client_builder``, then cached for the rest of the process
        lifetime.
        """
        cached = self._per_mailbox_graph.get(mailbox)
        if cached is not None:
            return cached
        built = self._client_builder(self._auth, mailbox)
        self._per_mailbox_graph[mailbox] = built
        return built

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """CredentialsConnector shim — return the input unchanged.

        Client-credentials flow consumes the operator-supplied tenant /
        client / secret triple as-is; no transformation, no token
        exchange at this surface (the OAuth2 helper exchanges at
        first-fetch time). Returning the input keeps the framework's
        credential-loading pass a no-op.
        """
        return credentials

    @classmethod
    def oauth_authorization_url(cls, _state: str) -> str:
        """OAuthConnector shim — raise actionable NotImplementedError.

        This connector uses the OAuth2 client-credentials flow (app-only,
        no operator-in-the-loop) per ADR-004 — there is no authorization
        URL to visit. The shim raises so a framework path that mistakenly
        routes to the three-legged flow fails loudly with a fix hint.
        """
        raise NotImplementedError(
            "m365_email_headers: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/m365_email_headers/connector.py for the credential contract."
        )

    @classmethod
    def oauth_code_to_token(cls, _code: str) -> dict[str, Any]:
        """OAuthConnector shim — raise actionable NotImplementedError.

        Counterpart to :meth:`oauth_authorization_url` — no code-to-token
        exchange because this connector does not surface an OAuth
        consent screen.
        """
        raise NotImplementedError(
            "m365_email_headers: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/m365_email_headers/connector.py for the credential contract."
        )

    # ------------------------------------------------------------------
    # Forward-only API (read by orchestration)
    # ------------------------------------------------------------------

    def next_cursor(self) -> str | None:
        """Return the deltaLink the orchestrator should persist after this tick.

        Populated by the most recent successful :meth:`list_changes`
        drain; ``None`` before the first call or when the Graph
        response carried no final deltaLink.
        """
        return self._next_cursor

    # ------------------------------------------------------------------
    # ADR-021 (Wave E.5) — per-source envelope metadata
    # ------------------------------------------------------------------

    def metadata_for(self, item_id: str) -> SourceMetadata:
        """Return the cached envelope metadata for ``item_id``.

        ADR-021: every Graph message envelope already carries sender +
        received-at + cc/to recipients before the orchestrator asks
        for ``fetch``. We surface ``from`` as author + author_email,
        ``receivedDateTime`` as modified_at, and ``cc`` recipients as
        tags. Cache miss collapses to an empty
        :class:`SourceMetadata`.
        """
        message = self._cache.get(item_id)
        if message is None:
            return SourceMetadata()
        sender = message.sender.strip() if message.sender else None
        author = sender
        author_email = sender if sender and "@" in sender else None
        properties: dict[str, str] = {}
        if message.subject:
            properties["subject"] = message.subject
        if message.sent_at:
            properties["sent_at"] = message.sent_at
        return SourceMetadata(
            modified_at=message.received_at or message.sent_at,
            created_at=message.sent_at,
            author=author,
            author_email=author_email,
            tags=tuple(message.to_recipients),
            properties=properties,
        )


def _decode_per_folder_cursor(cursor: str | None) -> dict[str, str]:
    """Decode a stored ``{folder_id: deltaLink}`` cursor string.

    The cursor on the wire is the JSON serialisation of a
    ``dict[str, str]``. Any value that isn't a JSON dict — including
    ``None``, an empty string, a non-JSON string, or a JSON value that
    isn't an object — collapses to an empty mapping, which the
    connector treats as cold-start.

    The "legacy / unrecognised" path is the safe migration for
    deployments that stored a single mailbox-wide deltaLink string
    before the #380 fix landed. Those cursors no longer match Graph's
    folder-scoped delta surface, so the connector restarts from cold —
    same observable state Graph would have reached if the operator
    never had a working cursor (which is exactly what #380 reports:
    Graph rejected mailbox-wide delta outright).
    """
    if not isinstance(cursor, str) or not cursor:
        return {}
    try:
        parsed = json.loads(cursor)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str):
            out[key] = value
    return out


def _encode_per_folder_cursor(cursors: Mapping[str, str]) -> str | None:
    """Encode a ``{folder_id: deltaLink}`` mapping as the stored cursor.

    Returns ``None`` for an empty mapping so the framework persists
    "no cursor" rather than the literal string ``"{}"`` — keeps the
    cursor column null when no folder has been drained yet.
    """
    if not cursors:
        return None
    return json.dumps(dict(cursors), sort_keys=True)


def _select_folders(
    folders: Sequence[MailFolderRef],
    allowlist: Sequence[str] | None,
) -> tuple[MailFolderRef, ...]:
    """Filter folders by the operator's optional allowlist.

    ``None`` / empty allowlist returns every folder. A populated
    allowlist matches:

      * well-known folder names (``inbox``, ``sentitems``, ``drafts``,
        ``deleteditems``, ``junkemail``, ``outbox``, ``archive``)
        case-insensitively against :attr:`MailFolderRef.well_known_name`
      * custom folder names case-insensitively against
        :attr:`MailFolderRef.display_name`

    Returns the folders in the order Graph surfaced them so the
    iteration order is stable across ticks (Graph's mailFolders
    response is documented stably-ordered).
    """
    if not allowlist:
        return tuple(folders)
    normalised = {entry.strip().lower() for entry in allowlist if entry and entry.strip()}
    if not normalised:
        return tuple(folders)
    out: list[MailFolderRef] = []
    for folder in folders:
        well_known = (folder.well_known_name or "").strip().lower()
        display = (folder.display_name or "").strip().lower()
        if (well_known and well_known in normalised) or (display and display in normalised):
            out.append(folder)
    return tuple(out)


def _event_modified_at(message: GraphMessage) -> str:
    """Pick the best timestamp for a ChangeEvent's ``modified_at``.

    Prefer the received timestamp (when the recipient inbox got the
    message — what the operator's timeline tracks); fall back to
    sent; fall back to wall-clock-now if Graph returned no envelope
    timestamps.
    """
    if message.received_at:
        return message.received_at
    if message.sent_at:
        return message.sent_at
    return _now_iso()


def _coerce_optional_string_list(
    raw: Any,
    *,
    error_message: str,
) -> list[str] | None:
    """Validate that ``raw`` is either ``None`` or a non-empty list/tuple of strings.

    Hoisted from :func:`make_connector` so the ``mailboxes`` + ``folders_allowlist``
    branches share one validation shape (Sonar S3776 — cuts the outer
    function's cognitive complexity by collapsing two near-identical
    blocks). Returns ``None`` when ``raw`` is ``None`` (caller treats
    that as "no override"); returns a freshly-copied ``list[str]``
    otherwise. Raises :class:`ValueError` with the supplied
    ``error_message`` when the shape is wrong (F21-shaped messages live
    at the call site so the diagnostic stays close to the docs link).

    Args:
      raw: The raw config value, typically ``config.get(<key>)``.
      error_message: The F21-shaped error string to surface on mis-shape.
    """
    if raw is None:
        return None
    if not isinstance(raw, list | tuple) or not all(isinstance(item, str) and item for item in raw):
        raise ValueError(error_message)
    return list(raw)


def make_connector(config: Mapping[str, Any]) -> M365EmailHeadersConnector:
    """Construct an :class:`M365EmailHeadersConnector` from a config mapping.

    Expected keys:

      * ``user_principal_name`` (required) — the target mailbox UPN.
      * ``sensitivity`` (optional) — ignored. Locked to ``personal``
        per ADR-004 + ADR-005; specifying a different tier in config
        is a config error (raises ``ValueError`` so operators see
        the misconfiguration loudly rather than silently mis-tagging).

    Credentials resolve via :class:`kairix.secrets.loader.SecretsLoader`
    against the canonical identities ``(connector, m365, None, tenant-id)``,
    ``(connector, m365, None, client-id)``, and ``(connector, m365, None,
    client-secret)``. The loader's legacy-alias fallback resolves the
    historical ``CONNECTOR_M365_*`` / ``KAIRIX_M365_*`` / ``M365_*`` env
    vars transparently, so existing deployments keep working unchanged.
    The OAuth2 client-credentials flow exchanges the triple for a bearer
    at Graph's ``v2.0/token`` endpoint.

    Registered via ``[project.entry-points."kairix.connectors"]`` in
    kairix's ``pyproject.toml`` so the orchestration layer can resolve
    ``m365_email_headers`` to this factory by name.
    """
    upn = config.get("user_principal_name")
    if not isinstance(upn, str) or not upn:
        raise ValueError(
            "m365_email_headers: config is missing 'user_principal_name'. "
            "fix: add user_principal_name: alice@contoso.com under the "
            "m365_email_headers connector block in kairix.config.yaml. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8."
        )

    declared_sensitivity = config.get(_SENSITIVITY_METADATA_KEY)
    if declared_sensitivity is not None and declared_sensitivity != LOCKED_SENSITIVITY:
        raise ValueError(
            f"m365_email_headers: sensitivity is locked to {LOCKED_SENSITIVITY!r} "
            f"per ADR-004 + ADR-005; config declared {declared_sensitivity!r}. "
            "fix: remove the sensitivity key from the m365_email_headers config "
            "block — the tier is set at the connector boundary. "
            "next: see docs/architecture/adrs/ADR-004-email-headers-only.md."
        )

    mailboxes = _coerce_optional_string_list(
        config.get("mailboxes"),
        error_message=(
            "m365_email_headers: 'mailboxes' must be a list of UPN strings. "
            "fix: write `mailboxes: [alice@contoso.com, bob@contoso.com]` "
            "under the m365_email_headers connector block. "
            "next: see docs/architecture/connector-scope-topology/ADR.md Wave E."
        ),
    )

    folders_allowlist = _coerce_optional_string_list(
        config.get("folders_allowlist"),
        error_message=(
            "m365_email_headers: 'folders_allowlist' must be a list of folder name strings. "
            "fix: write `folders_allowlist: [inbox, sentitems, archive]` "
            "under the m365_email_headers connector block. Well-known names "
            f"recognised: {sorted(_WELL_KNOWN_FOLDER_NAMES)!r}; custom folders "
            "match by case-insensitive displayName. "
            "next: see https://learn.microsoft.com/graph/api/resources/mailfolder "
            "for the well-known folder list."
        ),
    )

    return M365EmailHeadersConnector(
        user_principal_name=upn,
        mailboxes=mailboxes,
        folders_allowlist=folders_allowlist,
    )
