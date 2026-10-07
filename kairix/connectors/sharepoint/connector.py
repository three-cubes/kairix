"""``SharePointConnector`` — SourceConnector for SharePoint document libraries.

Implements :class:`kairix.core.protocols.SourceConnector` for one or
more SharePoint document libraries in a Microsoft 365 tenant. Change
detection rides the Graph drive delta-query token:

* First sync — no cursor — calls
  :meth:`SharePointGraphClient.iter_drive_items` from the seed delta
  URL for each configured drive. Every envelope surfaces as a
  ``created`` :class:`ChangeEvent`.
* Subsequent syncs — cursor is a persisted JSON map of
  ``drive_id -> deltaLink`` — calls
  :meth:`SharePointGraphClient.fetch_delta_page` from each drive's
  resume link. Envelopes surface as ``created`` / ``modified`` /
  ``deleted`` based on the delta payload.

Per ADR-019 (provider plugin architecture), this connector shares the
same Azure AD app registration with the M365 email-headers + calendar
siblings. The operator grants ``Sites.Read.All`` + ``Files.Read.All``
on the same app (alongside ``Mail.Read`` + ``Calendars.Read`` for the
siblings) and reuses the tenant/client/secret triple.

Out of scope for this slice (deferred to follow-up):

* Multi-container Wave E methods (``iter_containers`` / ``load_hierarchy``
  with per-drive emission) — today every drive in the configured set is
  driven through the single :meth:`list_changes` surface for
  back-compat with the legacy single-cursor pattern.
* SharePoint list items (only document libraries this slice).
* Per-Purview-label sensitivity routing — default ``internal`` with a
  ``default_sensitivity`` operator override.

Per F35, this module only imports from
``kairix.connectors.sharepoint.*`` (same plugin), ``kairix.core.*``
(the Protocol surface), and ``kairix.transport.auth.*`` (the shared
OAuth2 helper). No reach into other connectors, no reach into the
extractor layer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import httpx

from kairix.connectors.sharepoint.graph_client import (
    DriveItemRef,
    DriveRef,
    SharePointGraphClient,
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

logger = logging.getLogger(__name__)

CONNECTOR_NAME = "sharepoint"

# Default sensitivity tier for SharePoint content. SharePoint document
# libraries default to internal-tier corporate content; operators
# routing client-confidential or personal-tier libraries override via
# the connector config's ``default_sensitivity`` key.
DEFAULT_SENSITIVITY: Sensitivity = "internal"

# Microsoft Graph client-credentials scope for app-only reads.
# Always ``.default`` per the Microsoft v2 endpoint convention.
GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"

# Mime hint for binaries whose Graph envelope didn't declare one.
DEFAULT_FETCH_MIME = "application/octet-stream"

# Wave E hierarchy root node id. Each configured drive becomes a DRIVE-
# typed child FOLDER under this root SITE node.
_HIERARCHY_ROOT_ID = CONNECTOR_NAME
_HIERARCHY_ROOT_DISPLAY = "SharePoint"

# F17 — metadata key for the sensitivity tier carried on every emitted
# ChangeEvent. Extracted as a constant so the repeated literal across
# the legacy + Wave E emission paths has one edit site.
_META_SENSITIVITY_KEY = "sensitivity"

# PR#4 429 circuit-breaker. After this many drives exhaust the graph
# client's per-request retry budget with sustained throttling (429) in a
# single tick, the connector backs the WHOLE tick off for one cycle so a
# tenant-wide throttle stops thrashing the worker every
# CONNECTOR_SYNC_INTERVAL. Threshold > 1 so a single hot drive can't trip it.
_THROTTLE_BREAKER_THRESHOLD = 3
_THROTTLE_BACKOFF_SECONDS = 900  # one connector tick


def _now_iso() -> str:
    """Return a current ISO-8601 UTC timestamp matching the connector
    boundary's :class:`ChangeEvent.modified_at` format.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_after_s(seconds: int) -> str:
    """Return an ISO-8601 UTC timestamp ``seconds`` in the future.

    Stamps the 429 breaker's backoff-expiry in the same string shape as
    :func:`_now_iso`, so the two compare lexicographically.
    """
    future = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return future.isoformat().replace("+00:00", "Z")


def _is_throttle_exhaustion_error(exc: BaseException) -> bool:
    """True when ``exc``'s cause chain carries a 429 the retries couldn't clear.

    The graph client converts an exhausted-retry throttle into an
    :class:`httpx.HTTPStatusError` (status 429) via ``raise_for_status``;
    walk the ``__cause__`` / ``__context__`` chain so a wrapped 429 still
    counts toward the breaker while a transient 403 or timeout does not.
    """
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError) and current.response.status_code == 429:
            return True
        pending.append(current.__cause__)
        pending.append(current.__context__)
    return False


@dataclass(frozen=True)
class SharePointCredentials:
    """Resolved client-credentials triple for one SharePoint sync.

    Frozen per F42 — the dataclass is the typed shape that crosses the
    boundary between secret resolution and the connector constructor.
    Tests construct a literal :class:`SharePointCredentials` and pass it
    via the ``credentials`` kwarg; production resolves via the same
    pattern the sibling M365 connectors use.
    """

    tenant_id: str
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class SharePointDriveSpec:
    """One configured drive the connector should sync.

    Frozen per F42. ``drive_id`` is the Graph drive identifier; the
    operator obtains it once at deployment time (e.g. via the
    ``GET /sites?search=*`` enumeration call exposed by
    :meth:`SharePointGraphClient.list_sites`) and pins it in
    ``kairix.config.yaml``. Pinning by id (not by URL) makes the sync
    deterministic across site renames. To avoid pinning each drive by
    hand, name a SITE instead via :class:`SiteDiscoverySpec` and kairix
    auto-discovers every drive on that site at sync time.

    ``include_paths`` and ``exclude_paths`` scope which folders within
    the drive get indexed. Empty include_paths = whole drive. See
    ``docs/architecture/sharepoint-path-filtering.md`` for the semantics
    (segment-boundary prefix match, exclude wins, case-insensitive).
    """

    drive_id: str
    site_id: str | None = None
    display_name: str | None = None
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiteDiscoverySpec:
    """One configured SITE the connector should auto-discover drives from.

    Frozen per F42 (F42 — guided-config wizard / KFEAT-022 self-discovery
    foundation). A site discovery entry names a SharePoint site instead of
    a single drive; at SYNC time the connector enumerates every document
    library on that site (via
    :meth:`SharePointGraphClient.list_drives`) and syncs each one as if it
    had been declared explicitly. This restores the self-discovery the
    connector lost when it began requiring an explicit ``drive_id`` per
    drive — operators can now point at a site and let kairix find its
    libraries (including libraries added after the connector was first
    configured, because discovery re-runs every tick).

    Exactly one of ``site_id`` / ``site_url`` is required:

      * ``site_id`` — the Graph composite id
        (``<hostname>,<site-guid>,<web-guid>``). Deterministic across
        site renames; the preferred form.
      * ``site_url`` — a friendly site URL (e.g.
        ``https://contoso.sharepoint.com/sites/marketing``). Resolved to a
        ``site_id`` at sync time via
        :meth:`SharePointGraphClient.resolve_site_by_path`. Convenient for
        operators who don't have the composite id to hand.

    ``include_paths`` / ``exclude_paths`` are inherited by EVERY drive
    discovered under the site — the same path-filter semantics as
    :class:`SharePointDriveSpec` (segment-boundary prefix match, exclude
    wins, case-insensitive). Empty = whole drive.
    """

    site_id: str | None = None
    site_url: str | None = None
    include_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()


def _resolve_credentials_from_secrets(secrets: SecretsResolver) -> SharePointCredentials:
    """Resolve the three required secrets via the canonical :class:`SecretsResolver`.

    Per ADR-019, SharePoint reuses the M365 canonical identity tuple
    ``(connector, m365, None, <leaf>)`` so a single AAD app registration
    drives every sibling connector. Each call uses
    :meth:`SecretsResolver.require` so a missing secret raises
    :class:`kairix.secrets.SecretNotFoundError` with the canonical KV
    name + env-var in the message. The loader's legacy-alias fallback
    resolves the historical ``CONNECTOR_M365_*`` / ``KAIRIX_M365_*`` /
    ``M365_*`` env vars transparently so existing deployments keep
    working unchanged.
    """
    tenant = secrets.require("connector", "m365", None, "tenant-id")
    client = secrets.require("connector", "m365", None, "client-id")
    secret = secrets.require("connector", "m365", None, "client-secret")
    return SharePointCredentials(tenant_id=tenant, client_id=client, client_secret=secret)


class SharePointConnector:
    """SourceConnector for one or more SharePoint document libraries.

    Construction is cheap (no I/O, no OAuth exchange). The first
    :meth:`list_changes` call exchanges client-credentials for a bearer
    token (via the injected :class:`OAuth2ClientCredsAuth`) and drains
    each configured drive's delta query in turn.

    DI seams:

      * ``credentials`` — resolved :class:`SharePointCredentials`. Tests
        pass a literal; production callers omit and the factory resolves
        from :mod:`kairix.secrets`.
      * ``client_builder`` — builds the :class:`SharePointGraphClient`.
        Tests pass a builder returning a client backed by an
        ``httpx.MockTransport`` so no real Graph call leaks.
      * ``auth`` — pre-built :class:`OAuth2ClientCredsAuth`. Tests pass
        an auth bound to the mock-transport client; production omits
        and the connector builds one from ``credentials``.
      * ``default_sensitivity`` — connector-wide F39 tier; defaults to
        ``internal`` per ADR-005. Operators set the matching key in
        ``connector_specific_config`` to override.
      * ``max_delta_pages_per_tick`` — optional Graph-page cap. When set, the
        connector stops after that many delta pages and persists the page's
        ``nextLink`` so the next worker tick resumes without skipping items.
    """

    name: str = CONNECTOR_NAME
    # SharePoint cursors are Graph page links, not item offsets. The connector
    # owns bounded yielding at page boundaries; the generic item-count pipeline
    # budget must not cut a page after ``next_cursor`` has moved past it.
    per_tick_max_items: int = 1_000_000
    disk_watermark_min_free_bytes: int | None = 5 * 1024**3  # 5 GiB — SharePoint blobs can be large

    def __init__(
        self,
        drives: list[SharePointDriveSpec],
        *,
        site_discovery: list[SiteDiscoverySpec] | None = None,
        credentials: SharePointCredentials | None = None,
        client_builder: Callable[[OAuth2ClientCredsAuth], SharePointGraphClient] | None = None,
        auth: OAuth2ClientCredsAuth | None = None,
        default_sensitivity: Sensitivity = DEFAULT_SENSITIVITY,
        secrets: SecretsResolver | None = None,
        max_delta_pages_per_tick: int | None = None,
    ) -> None:
        if max_delta_pages_per_tick is not None and max_delta_pages_per_tick < 1:
            raise ValueError(
                "sharepoint: max_delta_pages_per_tick must be a positive integer. "
                "fix: set max_delta_pages_per_tick to an integer >= 1, or omit it to drain all pages. "
                "next: use this knob for first-sync backfills where one SharePoint site is starving sibling connectors."
            )
        site_specs = tuple(site_discovery or ())
        if not drives and not site_specs:
            raise ValueError(
                "sharepoint: drives list is empty. "
                "fix: declare at least one drive_id, or a site (site_id) to "
                "auto-discover its drives, under the sharepoint connector block. "
                "next: see docs/architecture/connector-ingestion-architecture.md §8 "
                "for the SharePoint connector config shape."
            )
        # Explicit drive specs the operator pinned by id. Site-discovery
        # specs are expanded LAZILY at sync time (each tick, so newly
        # added libraries are picked up) — never here, so construction
        # stays I/O-free and one site's transient failure can't crash init.
        self._explicit_drives: tuple[SharePointDriveSpec, ...] = tuple(drives)
        self._site_specs: tuple[SiteDiscoverySpec, ...] = site_specs
        self._default_sensitivity: Sensitivity = default_sensitivity
        self._max_delta_pages_per_tick = max_delta_pages_per_tick

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

        if client_builder is not None:
            self._graph = client_builder(resolved_auth)
        else:
            self._graph = SharePointGraphClient(auth=resolved_auth)

        # Per-item envelope cache — populated by :meth:`list_changes`
        # so :meth:`fetch` can resolve drive id, web URL, and mime
        # without a second Graph call.
        self._cache: dict[str, DriveItemRef] = {}
        # Per-TICK resolved-drive-spec cache — explicit drives + every
        # drive discovered from configured sites, keyed by drive id.
        # Site discovery (``GET /sites/{id}/drives``) is expensive (one
        # Graph call per site); resolving it ONCE per tick and reusing the
        # map keeps a deployment with N drives + M sites at M discovery
        # calls per tick instead of NxM. ``None`` means "not yet resolved
        # this tick" — the tick entry points (:meth:`list_changes`,
        # :meth:`iter_containers`, :meth:`load_hierarchy`) refresh it so a
        # newly-added library is still picked up each tick; the
        # per-container lookups read it without re-running discovery.
        self._resolved_spec_map: dict[str, SharePointDriveSpec] | None = None
        # Next-tick cursor — populated after :meth:`list_changes` drains
        # every configured drive. Serialised as a JSON map
        # ``drive_id -> deltaLink`` so a single opaque string round-trips
        # through the orchestrator's cursor_store.
        self._next_cursor: str | None = None
        # PR#4 429 breaker state: count of drives that exhausted retries with
        # sustained throttling this tick, and the ISO timestamp until which the
        # breaker holds the connector off (None = breaker not tripped).
        self._throttle_hits_this_tick = 0
        self._throttle_backoff_until: str | None = None

        # Probe each EXPLICIT include_path against the live drive at
        # startup so missing folders surface proactively (not silently,
        # the first time a tick rejects every item). One-shot per process;
        # transient Graph errors don't kill init. Site-discovery specs are
        # NOT probed here — their drives aren't resolved until the first
        # sync (lazy discovery), so probing them would force a Graph call
        # at construction time.
        self._probe_include_paths()

    # ------------------------------------------------------------------
    # Drive-spec resolution — explicit + lazily-discovered site drives
    # ------------------------------------------------------------------

    @property
    def _drives(self) -> tuple[SharePointDriveSpec, ...]:
        """Explicit operator-pinned drive specs (back-compat accessor).

        Site-discovery drives are intentionally NOT included here — they
        resolve lazily at sync time via :meth:`_resolve_drive_specs`. The
        startup probe and any code that must avoid a Graph call read this
        property; the sync paths read the resolved set instead.
        """
        return self._explicit_drives

    def _resolve_drive_specs(self) -> tuple[SharePointDriveSpec, ...]:
        """Resolve explicit drives + discovered site drives ONCE per tick.

        Site discovery (``GET /sites/{id}/drives``) is expensive — one
        Graph call per configured site. This method runs discovery
        exactly once per tick and memoises the result on
        :attr:`_resolved_spec_map`; the per-container lookups
        (:meth:`_spec_for_drive_id`) read that map instead of re-running
        discovery, so a deployment with N drives + M sites makes M
        discovery calls per tick, not NxM.

        The tick entry points (:meth:`list_changes`,
        :meth:`iter_containers`, :meth:`load_hierarchy`) call
        :meth:`_refresh_resolved_specs` to invalidate the cache at the
        start of each tick, so libraries added to a site after the
        connector was configured are still picked up automatically. The
        per-container Wave-E paths that may be entered without a fresh
        tick boundary fall through to a lazy one-shot resolve here.

        Each :class:`SiteDiscoverySpec` is expanded independently with
        per-site fault isolation — a site that fails to resolve (not
        found / auth / throttle / empty) is logged and skipped WITHOUT
        affecting the explicit drives or the other sites (per-site fault
        isolation, F42 robustness).
        """
        if self._resolved_spec_map is not None:
            return tuple(self._resolved_spec_map.values())
        return self._refresh_resolved_specs()

    def _refresh_resolved_specs(self) -> tuple[SharePointDriveSpec, ...]:
        """Re-run site discovery and rebuild the per-tick resolved-spec map.

        Called at the start of each tick entry point so newly-added
        libraries are picked up; populates :attr:`_resolved_spec_map`
        (keyed by drive id) so the per-container lookups in the same tick
        reuse it instead of re-running discovery (NxM → M calls/tick).
        """
        resolved: dict[str, SharePointDriveSpec] = {}
        for spec in self._explicit_drives:
            resolved.setdefault(spec.drive_id, spec)
        for site_spec in self._site_specs:
            for discovered in self._discover_site_drives(site_spec):
                resolved.setdefault(discovered.drive_id, discovered)
        self._resolved_spec_map = resolved
        return tuple(resolved.values())

    def _spec_for_drive_id(self, drive_id: str) -> SharePointDriveSpec | None:
        """Return the resolved spec for ``drive_id`` (explicit or discovered).

        Used by the per-container paths whose ``container_id`` may name a
        drive discovered from a site, so the container inherits the
        site's path filters. Reads the per-tick resolved-spec map (built
        once per tick by :meth:`_resolve_drive_specs`) so a per-container
        lookup never re-runs site discovery. Returns ``None`` when no
        resolved spec matches (e.g. the site that produced the drive
        failed discovery this tick) — the caller then drains the drive
        unfiltered.
        """
        if self._resolved_spec_map is None:
            self._resolve_drive_specs()
        assert self._resolved_spec_map is not None  # populated by the resolve above
        return self._resolved_spec_map.get(drive_id)

    def _discover_site_drives(self, site_spec: SiteDiscoverySpec) -> list[SharePointDriveSpec]:
        """Discover one site's drives, applying the site's path filters.

        Robust by contract: any failure resolving the site id, listing
        its drives, or an empty drive set is logged as a structured WARN
        naming the site and an empty list is returned so the caller skips
        that site this tick. Never raises — one site's outage must not
        crash the connector or freeze unrelated cursors.
        """
        site_label = site_spec.site_id or site_spec.site_url or "<unknown>"
        try:
            site_id = self._resolve_site_id(site_spec)
            if site_id is None:
                return []
            drives = [
                self._drive_spec_from_ref(ref, site_spec=site_spec)
                for ref in self._graph.list_drives(site_id)
                if ref.drive_id
            ]
        except Exception as exc:
            logger.warning(
                "event=sharepoint_discover_error site=%s error=%s "
                "(this site skipped this tick; explicit drives + other sites still sync; "
                "next tick will retry)",
                site_label,
                exc,
            )
            return []
        if not drives:
            logger.warning(
                "event=sharepoint_discover_no_drives site=%s. "
                "fix: confirm the site has at least one document library the app can read "
                "(Sites.Read.All + Files.Read.All), or pin explicit drive_ids instead. "
                "next: the connector retries discovery on the next tick.",
                site_label,
            )
        return drives

    def _resolve_site_id(self, site_spec: SiteDiscoverySpec) -> str | None:
        """Resolve a site spec to a Graph composite site id.

        Prefers an explicit ``site_id``; otherwise resolves ``site_url``
        via :meth:`SharePointGraphClient.resolve_site_by_path`. Returns
        ``None`` (after a structured WARN) when neither yields a usable
        id so the caller skips the site this tick.
        """
        if site_spec.site_id:
            return site_spec.site_id
        if site_spec.site_url:
            return self._resolve_site_id_from_url(site_spec.site_url)
        return None

    def _resolve_site_id_from_url(self, site_url: str) -> str | None:
        """Resolve a friendly ``site_url`` to a Graph composite site id.

        Two skip-with-WARN paths, both isolated so the site is dropped
        this tick rather than producing a bad/empty site id downstream:

          * the URL has no host or server-relative path
            (:func:`_split_site_url` returns ``(None, None)``) — WARN
            ``sharepoint_discover_site_url_unparseable``;
          * the URL parses but Graph resolves it to an empty/None site id
            (deleted site, permission gap) — WARN
            ``sharepoint_discover_site_unresolved``. Previously this empty
            resolution was swallowed silently, leaking a falsy site id
            into discovery.
        """
        hostname, server_relative_path = _split_site_url(site_url)
        if hostname is None or server_relative_path is None:
            logger.warning(
                "event=sharepoint_discover_site_url_unparseable site_url=%s. "
                "fix: use a full site URL like "
                "'https://contoso.sharepoint.com/sites/marketing', or pin site_id directly. "
                "next: the connector retries discovery on the next tick.",
                site_url,
            )
            return None
        resolved_site_id = self._graph.resolve_site_by_path(hostname, server_relative_path).site_id
        if not resolved_site_id:
            logger.warning(
                "event=sharepoint_discover_site_unresolved site_url=%s. "
                "fix: confirm the site URL is correct and the app can read it "
                "(Sites.Read.All), or pin the site_id (composite id) directly. "
                "next: the connector retries discovery on the next tick.",
                site_url,
            )
            return None
        return resolved_site_id

    def _drive_spec_from_ref(self, ref: DriveRef, *, site_spec: SiteDiscoverySpec) -> SharePointDriveSpec:
        """Map a discovered :class:`DriveRef` to a concrete drive spec.

        Carries the library name from the ref as the display name (so
        status surfaces show a real label, not a drive-id prefix) and
        inherits the site spec's ``include_paths`` / ``exclude_paths`` so
        the operator's site-level folder scope applies to every drive.
        """
        return SharePointDriveSpec(
            drive_id=ref.drive_id,
            site_id=ref.site_id or site_spec.site_id,
            display_name=ref.name or None,
            include_paths=site_spec.include_paths,
            exclude_paths=site_spec.exclude_paths,
        )

    # ------------------------------------------------------------------
    # SourceConnector Protocol surface
    # ------------------------------------------------------------------

    def list_changes(self, cursor: Cursor | None) -> Iterator[ChangeEvent]:
        """Stream changes across every configured + discovered drive.

        ``cursor`` is the JSON map persisted on the previous tick (or
        ``None`` for cold start). The connector resolves the live drive
        set (explicit drives + every drive discovered from configured
        sites), walks each drive's delta endpoint, caches envelopes on
        the way through, and records the next-tick cursor on
        :attr:`_next_cursor`. The per-drive cursor is keyed on the
        (possibly discovered) ``drive_id`` so it round-trips through the
        existing deltaLink map regardless of how the drive was declared.
        """
        if self._throttle_breaker_active():
            logger.warning(
                "event=sharepoint_breaker_active name=%s backoff_until=%s "
                "(sustained 429 last tick; skipping this tick so Graph recovers; "
                "cursor unchanged, next tick will retry)",
                self.name,
                self._throttle_backoff_until,
            )
            self._next_cursor = cursor if isinstance(cursor, str) else None
            return iter([])
        per_drive_cursor = _deserialise_cursor(cursor)
        events: list[ChangeEvent] = []
        next_links: dict[str, str] = {}
        self._throttle_hits_this_tick = 0
        for spec in self._refresh_resolved_specs():
            drive_id = spec.drive_id
            start_url = per_drive_cursor.get(drive_id)
            try:
                drive_delta = self._drain_drive(spec, start_url, events)
            except Exception as exc:
                self._record_drive_failure(drive_id, exc, start_url, next_links)
                continue
            if drive_delta is not None:
                next_links[drive_id] = drive_delta
            elif start_url is not None:
                next_links[drive_id] = start_url
        self._maybe_trip_throttle_breaker()
        self._next_cursor = _serialise_cursor(next_links) if next_links else None
        return iter(events)

    def _stage_page_items(
        self,
        items: tuple[DriveItemRef, ...],
        spec: SharePointDriveSpec,
        staged: list[ChangeEvent],
        staged_cache: dict[str, DriveItemRef],
    ) -> None:
        """Stage one delta page's in-scope items as events + cache entries.

        Items filtered out by ``spec`` or that map to no event are skipped;
        nothing is merged into the live cache here — :meth:`_drain_drive`
        commits the staged set only once the drive drains.
        """
        for item in items:
            if not self._item_passes_spec_filter(item, spec=spec):
                continue
            event = self._item_to_event(item, drive_id=spec.drive_id)
            if event is None:
                continue
            staged_cache[event.item_id] = item
            staged.append(event)

    def _drain_drive(
        self,
        spec: SharePointDriveSpec,
        start_url: str | None,
        events: list[ChangeEvent],
    ) -> str | None:
        """Drain one drive's delta into ``events`` atomically; return its deltaLink.

        Changes are staged locally and only merged into ``events`` + the
        envelope cache once the drive drains in full. A mid-iteration failure
        (e.g. a throttle on delta page 2) therefore leaves NO partial events
        or stale cache entries behind — :meth:`list_changes` wraps the call,
        carries the drive's prior cursor forward, and the next tick re-drains
        the whole drive cleanly.
        """
        drive_id = spec.drive_id
        if not hasattr(self._graph, "fetch_delta_page") or not hasattr(self._graph, "initial_delta_url"):
            return self._drain_drive_via_legacy_iterator(spec, start_url, events)

        staged: list[ChangeEvent] = []
        staged_cache: dict[str, DriveItemRef] = {}
        url: str | None = start_url or self._graph.initial_delta_url(drive_id)
        pages_seen = 0
        last_delta_link: str | None = None
        while url is not None:
            page = self._graph.fetch_delta_page(url)
            pages_seen += 1
            self._stage_page_items(page.items, spec, staged, staged_cache)
            if page.delta_link is not None:
                last_delta_link = page.delta_link
            if (
                self._max_delta_pages_per_tick is not None
                and pages_seen >= self._max_delta_pages_per_tick
                and page.next_link is not None
            ):
                self._cache.update(staged_cache)
                events.extend(staged)
                return page.next_link
            url = page.next_link
        self._cache.update(staged_cache)
        events.extend(staged)
        return last_delta_link

    def _drain_drive_via_legacy_iterator(
        self,
        spec: SharePointDriveSpec,
        start_url: str | None,
        events: list[ChangeEvent],
    ) -> str | None:
        """Legacy graph-client path for iterator-only seams.

        The production Graph client exposes page-level delta calls so the
        connector can stop on a safe ``nextLink`` boundary. Older tests and
        embedders only provide ``iter_drive_items`` plus an optional
        ``last_delta_link_for_drive`` accessor; keep that public protocol
        working while preserving the same atomic per-drive staging.
        """
        drive_id = spec.drive_id
        staged: list[ChangeEvent] = []
        staged_cache: dict[str, DriveItemRef] = {}
        for item in self._graph.iter_drive_items(drive_id, start_url=start_url):
            if not self._item_passes_spec_filter(item, spec=spec):
                continue
            event = self._item_to_event(item, drive_id=drive_id)
            if event is None:
                continue
            staged_cache[event.item_id] = item
            staged.append(event)
        self._cache.update(staged_cache)
        events.extend(staged)
        get_delta = getattr(self._graph, "last_delta_link_for_drive", None)
        return cast(str | None, get_delta(drive_id)) if callable(get_delta) else None

    def _record_drive_failure(
        self,
        drive_id: str,
        exc: Exception,
        start_url: str | None,
        next_links: dict[str, str],
    ) -> None:
        """Log a per-drive failure and carry its cursor forward for a retry.

        A drive that raised is skipped this tick; its prior cursor (when it
        had one) is re-persisted so the next tick resumes from the same
        position. Counts toward the 429 breaker only when the error chain
        carries an exhausted-retry 429.
        """
        if _is_throttle_exhaustion_error(exc):
            self._throttle_hits_this_tick += 1
        if start_url is not None:
            next_links[drive_id] = start_url
        logger.warning(
            "event=sharepoint_drive_error name=%s drive=%s error=%s "
            "(this drive skipped this tick; other drives still sync; "
            "next tick will retry)",
            self.name,
            drive_id,
            exc,
        )

    def _throttle_breaker_active(self) -> bool:
        """Return True while the 429 breaker is holding the connector off.

        Clears the breaker (and returns False) once the backoff window set by
        :meth:`_maybe_trip_throttle_breaker` has elapsed.
        """
        if self._throttle_backoff_until is None:
            return False
        if _now_iso() >= self._throttle_backoff_until:
            self._throttle_backoff_until = None
            return False
        return True

    def _maybe_trip_throttle_breaker(self) -> None:
        """Trip the 429 breaker when this tick's throttle count hit the threshold."""
        if self._throttle_hits_this_tick < _THROTTLE_BREAKER_THRESHOLD:
            return
        self._throttle_backoff_until = _timestamp_after_s(_THROTTLE_BACKOFF_SECONDS)
        logger.warning(
            "event=sharepoint_breaker_tripped name=%s drives_throttled=%d backoff_until=%s "
            "(connector backing off one tick to let Graph recover; cursor not advanced)",
            self.name,
            self._throttle_hits_this_tick,
            self._throttle_backoff_until,
        )

    def fetch(self, item_id: str) -> RawArtefact:
        """Download the binary content for ``item_id``.

        Uses the per-tick envelope cache populated by
        :meth:`list_changes` to resolve the drive id; raises with a
        fix-pointer when the orchestrator asks for an id outside the
        cache (typically because ``fetch`` was called without a prior
        ``list_changes`` drain in this process).
        """
        envelope = self._cache.get(item_id)
        if envelope is None:
            raise KeyError(
                f"sharepoint: item_id {item_id!r} not in the per-tick envelope cache. "
                "fix: call list_changes() before fetch() so the delta drain "
                "populates the envelope cache before the orchestrator asks for the body. "
                "next: see kairix/core/connectors/pipeline.py for the orchestrator's "
                "list_changes -> fetch contract."
            )
        raw = self._graph.fetch_item_content(envelope.drive_id, envelope.item_id)
        mime = envelope.mime or DEFAULT_FETCH_MIME
        return RawArtefact(raw=raw, mime=mime, fetched_at=_now_iso())

    def source_link(self, item_id: str) -> str:
        """Return the SharePoint web URL for the cached envelope.

        Falls back to a Graph drive-item URI shape when the envelope
        didn't carry a web URL (older Graph responses, or items whose
        canonical URL is computed lazily by SharePoint). The fallback
        is still deterministic and round-trips back to the source via
        the Graph items endpoint.
        """
        envelope = self._cache.get(item_id)
        if envelope is not None and envelope.web_url:
            return envelope.web_url
        if envelope is not None:
            return f"sharepoint://{envelope.drive_id}/items/{envelope.item_id}"
        return f"sharepoint://items/{item_id}"

    def sensitivity_for(self, _item_id: str) -> Sensitivity:
        """Return the connector-configured default sensitivity.

        v1 has no per-item overrides — every envelope from the connector
        carries the configured tier. A future ADR can read Microsoft
        Purview labels off the envelope and downgrade specific items
        without breaking the Protocol.
        """
        return self._default_sensitivity

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
        """CheckpointedConnector shim — forward to :meth:`list_changes`.

        Graph delta works on opaque deltaLink strings (serialised as a
        per-drive JSON map by this connector); the shim forwards
        ``checkpoint`` directly so observable behaviour matches the v1
        path. ``_container`` is accepted for Protocol compliance but the
        legacy path is single-cursor per cc_pair (Wave E activates
        per-container routing).
        """
        return self.list_changes(checkpoint)

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:
        """CredentialsConnector shim — return the input unchanged.

        Client-credentials flow consumes the operator-supplied tenant /
        client / secret triple as-is; no transformation, no token
        exchange at this surface (the OAuth2 helper exchanges at
        first-fetch time).
        """
        return credentials

    @classmethod
    def oauth_authorization_url(cls, _state: str) -> str:
        """OAuthConnector shim — raise actionable NotImplementedError.

        This connector uses the OAuth2 client-credentials flow (app-only,
        no operator-in-the-loop) — there is no authorization URL to
        visit. The shim raises so a framework path that mistakenly
        routes to the three-legged flow fails loudly with a fix hint.
        """
        raise NotImplementedError(
            "sharepoint: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/sharepoint/connector.py for the credential contract."
        )

    @classmethod
    def oauth_code_to_token(cls, _code: str) -> dict[str, Any]:
        """OAuthConnector shim — raise actionable NotImplementedError.

        Counterpart to :meth:`oauth_authorization_url` — no code-to-token
        exchange because this connector does not surface an OAuth
        consent screen.
        """
        raise NotImplementedError(
            "sharepoint: client-credentials flow only; OAuth user flow not supported for this plugin. "
            "fix: drive auth via the configured tenant_id / client_id / client_secret triple. "
            "next: see kairix/connectors/sharepoint/connector.py for the credential contract."
        )

    # ------------------------------------------------------------------
    # Topology Wave E — per-connector multi-container pilot
    # ------------------------------------------------------------------
    # Wave B landed shim implementations of the capability Protocols
    # (CheckpointedConnector / CredentialsConnector / OAuthConnector).
    # Wave E adds real implementations behind the
    # ``topology_sharepoint`` flag:
    #
    #   * :meth:`iter_containers` — one :class:`Container` per configured
    #     Graph drive, each with its own ``@odata.deltaLink`` persisted
    #     as the container's ``cursor_token`` (replaces the v1 single
    #     packed JSON map).
    #   * :meth:`list_changes_for_container` — when flag ON, reads
    #     ``container.cursor_token`` (a per-drive Graph deltaLink) and
    #     runs the Graph delta query against ``container.container_id``
    #     (the drive id) ONLY. When flag OFF, retains the Wave B shim
    #     behaviour (delegate to legacy :meth:`list_changes`).
    #   * :meth:`load_hierarchy` — when flag ON, emits a root SITE-typed
    #     FOLDER node plus one DRIVE-typed FOLDER per configured drive
    #     parent-before-child per F58. When flag OFF, emits one root
    #     FOLDER node only (Wave B shim shape).
    #   * :meth:`retrieve_all_slim_docs` — id-only enumeration for the
    #     prune cycle; drains the per-container delta with envelope
    #     items only.
    #   * :meth:`reindex` — :class:`Resolver` — per-item failure replay;
    #     emits one :class:`ChangeEvent` per failed item id without
    #     re-running the full delta window.
    #
    # The flag defaults OFF so existing operators see bit-for-bit
    # current behaviour. The ON branch is the per-container pattern
    # that mirrors the obsidian / m365_calendar / m365_email_headers
    # Wave E pilots.

    def iter_containers(self, cc_pair_id: int) -> Iterator[Container]:
        """Yield one :class:`Container` per configured Graph drive.

        Topology §4: each Container has its own delta cursor — the
        Wave E pilot maps each operator-declared drive to its own
        Container so the operator can add or remove individual drives
        without disturbing the cursor state of the others.

        ``access_state`` is always ``ACCESSIBLE`` at iteration time;
        per-drive permission drift (Sites.Selected revocation) surfaces
        as a request-time error from :meth:`list_changes_for_container`,
        not at iteration. ``cursor_token`` and ``last_synced_at`` start
        ``None``; the framework persists subsequent values (the Graph
        ``@odata.deltaLink``) to the ``topology_containers`` table.

        Calling convention mirrors the sibling Wave E pilots: the
        framework's lifecycle layer (``kairix/core/connectors/cc_pair.py``)
        passes ``cc_pair_id`` so the connector can construct the
        Container without reaching back into the cc_pair store.

        Site-discovery drives surface here too — each drive discovered
        from a configured site becomes its own Container (resolved fresh
        each call so newly-added libraries appear automatically).

        This is a TICK entry point: it refreshes the per-tick
        resolved-spec map once (one discovery call per site) so the
        per-container :meth:`list_changes_for_container` /
        :meth:`retrieve_all_slim_docs` calls that follow reuse it instead
        of each re-running discovery (NxM → M discovery calls per tick).
        """
        for spec in self._refresh_resolved_specs():
            yield Container(
                cc_pair_id=cc_pair_id,
                container_id=spec.drive_id,
                access_state="ACCESSIBLE",
                cursor_token=None,
                last_synced_at=None,
            )

    def list_changes_for_container(self, container: Container) -> Iterator[ChangeEvent]:
        """Stream changes for one container's Graph drive.

        Reads ``container.cursor_token`` as the per-drive Graph
        deltaLink (None on first sync) and walks the delta pages for
        THAT drive only. Per-drive isolation means adding or removing
        one drive does not affect the cursor state of the others —
        bypasses the legacy packed JSON cursor map entirely so a
        single-drive 403 cannot poison the shared cursor.

        ``topology_sharepoint`` retired post-cutover (task #132);
        the per-drive path is now the only behaviour.
        """
        return self._list_changes_for_container_scoped(container)

    def load_hierarchy(self, cc_pair_id: int) -> Iterator[HierarchyNode]:
        """HierarchyConnector — emit nodes parent-before-child per F58.

        Emits a root SITE-typed FOLDER node (``raw_node_id="sharepoint"``,
        ``raw_parent_id=None``) followed by one DRIVE-typed FOLDER per
        configured drive, with ``raw_node_id`` set to the drive id and
        ``raw_parent_id`` pointing at the root. Parent-before-child per
        F58.

        Per-drive sub-folder hierarchy (Documents / Shared with me /
        custom libraries) is a later-wave enhancement — this slice keeps
        the hierarchy at drive-as-folder granularity.

        ``topology_sharepoint`` retired post-cutover (task #132);
        the SITE + DRIVE per-drive emission is now the only behaviour.

        Site-discovery drives appear as DRIVE nodes too — the drive set
        is resolved fresh (explicit + discovered) so the hierarchy
        reflects libraries added to a site since configuration.
        """
        yield HierarchyNode(
            cc_pair_id=cc_pair_id,
            raw_node_id=_HIERARCHY_ROOT_ID,
            raw_parent_id=None,
            display_name=_HIERARCHY_ROOT_DISPLAY,
            link=None,
            node_type="SITE",
            external_access_json=None,
            sensitivity_hint=None,
        )
        for spec in self._refresh_resolved_specs():
            yield HierarchyNode(
                cc_pair_id=cc_pair_id,
                raw_node_id=spec.drive_id,
                raw_parent_id=_HIERARCHY_ROOT_ID,
                display_name=self._effective_display_name(spec),
                link=None,
                node_type="DRIVE",
                external_access_json=None,
                sensitivity_hint=None,
            )

    def retrieve_all_slim_docs(self, container: Container) -> Iterator[str]:
        """SlimConnector — id-only enumeration for the prune cycle.

        Drains the per-container delta endpoint (or full enumeration
        when the container's cursor is None) and emits only the
        ``item_id`` strings. The orchestrator diffs this against the
        ``documents`` table to detect deletes and stage tombstones —
        much cheaper than re-fetching every body.

        Reads ``container.cursor_token`` so the prune scan honours the
        per-container resume position; ``None`` triggers a full
        enumeration (cold-prune). Filters tombstones (removed items)
        out because the prune cycle is asking "what ids does the source
        still have?".
        """
        drive_id = container.container_id
        start_url = container.cursor_token
        spec = self._spec_for_drive_id(drive_id)
        for item in self._graph.iter_drive_items(drive_id, start_url=start_url):
            if not item.item_id or item.removed:
                continue
            if spec is not None and not self._item_passes_spec_filter(item, spec=spec):
                continue
            yield item.item_id

    def reindex(
        self,
        failed_item_ids: tuple[str, ...],
        *,
        include_permissions: bool = False,
    ) -> Iterator[ChangeEvent]:
        """Resolver — per-item failure replay.

        Cheaper than re-running a delta window after a partial-fetch
        failure: yields one :class:`ChangeEvent` per id in
        ``failed_item_ids`` so the orchestrator can re-drive the
        downstream pipeline (fetch → extract → silver → index) against
        ONLY the items that failed.

        Each emitted event is shaped as a ``modified`` op (the item
        existed before the failure and still exists; reindex is a
        replay of the silver/index path, not a tombstone scan). The
        event's ``modified_at`` carries the wall-clock at replay time
        so any downstream recency-sort sees the replay as recent.

        ``include_permissions`` is accepted per the Protocol surface
        but the Wave E slice ships only the bare reindex path —
        permission-replay layers on top when SlimConnectorWithPermSync
        lands in a follow-up slice. The kwarg is recorded in metadata
        so a future slice can route to the perm-sync replay without a
        Protocol break.

        Filters duplicate ids and empty strings so the orchestrator's
        deadletter table can safely feed the raw tuple without
        pre-cleaning. The "replay only failed ids" filter is the
        load-bearing invariant — sabotage-proved by integration
        coverage that asserts the emitted ids match the failures tuple
        and nothing else.
        """
        seen: set[str] = set()
        for raw_id in failed_item_ids:
            if not raw_id or raw_id in seen:
                continue
            seen.add(raw_id)
            yield ChangeEvent(
                op="modified",
                item_id=raw_id,
                modified_at=_now_iso(),
                metadata={
                    _META_SENSITIVITY_KEY: self._default_sensitivity,
                    "reindex": True,
                    "include_permissions": include_permissions,
                },
            )

    # ------------------------------------------------------------------
    # Forward-only API
    # ------------------------------------------------------------------

    def next_cursor(self) -> str | None:
        """Return the JSON cursor map the orchestrator should persist.

        Populated by the most recent successful :meth:`list_changes`
        drain; ``None`` before the first call or when no drive completed
        a delta sweep.
        """
        return self._next_cursor

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _list_changes_for_container_scoped(self, container: Container) -> Iterator[ChangeEvent]:
        """Wave E ON-branch: drain Graph delta for one container's drive only.

        Reads the container's own ``cursor_token`` (the per-drive Graph
        ``@odata.deltaLink``) and walks delta pages for the container's
        drive id only. Each container's cursor is read independently —
        adding or removing one drive does not disturb another drive's
        resume position.

        Bypasses the legacy ``_serialise_cursor`` / ``_deserialise_cursor``
        packed JSON map entirely so a single-drive failure cannot
        poison the shared cursor. The per-container path's events still
        populate ``self._cache`` so :meth:`fetch` can resolve the drive
        id without a second Graph call.
        """
        drive_id = container.container_id
        start_url = container.cursor_token
        spec = self._spec_for_drive_id(drive_id)
        events: list[ChangeEvent] = []
        for item in self._graph.iter_drive_items(drive_id, start_url=start_url):
            if spec is not None and not self._item_passes_spec_filter(item, spec=spec):
                continue
            event = self._item_to_event(item, drive_id=drive_id)
            if event is None:
                continue
            self._cache[event.item_id] = item
            events.append(event)
        return iter(events)

    def _effective_display_name(self, spec: SharePointDriveSpec) -> str:
        """Return the operator-facing label for a drive spec.

        Resolution order:
          1. Operator-provided ``display_name`` — used verbatim
          2. Spec with non-empty ``include_paths`` → synthesise
             ``"<drive-id-prefix> [<first-include-path>]"`` so two specs
             against the same drive but different include paths are
             distinguishable in status surfaces (`kairix features status`,
             `tool_features_status`, structured logs)
          3. Fall back to ``drive_id`` (legacy behaviour preserved when
             the operator hasn't set a name and hasn't applied a filter)

        The drive-id prefix is the first 8 chars + ellipsis — Graph drive
        ids are 60+ chars of base64 and unreadable in full; the prefix
        gives a stable handle without overwhelming the label. Operators
        who want the actual SharePoint drive name set ``display_name``
        explicitly.
        """
        if spec.display_name:
            return spec.display_name
        if spec.include_paths:
            short = (spec.drive_id[:8] + "…") if len(spec.drive_id) > 8 else spec.drive_id
            return f"{short} [{spec.include_paths[0]}]"
        return spec.drive_id

    def _item_passes_spec_filter(self, item: DriveItemRef, *, spec: SharePointDriveSpec) -> bool:
        """True when the item should pass through the spec's path filter.

        When include / exclude paths are both empty, this is a no-op
        (returns True for every item). When either is set, items whose
        Graph envelope omitted ``parentReference.path`` are dropped and
        a debug log emitted so surprise misses are grep-able.
        """
        if not spec.include_paths and not spec.exclude_paths:
            return True
        item_path = _full_item_path(item)
        if item_path is None:
            logger.debug(
                "event=sharepoint_filter_dropped_no_path drive=%s item_id=%s name=%s",
                spec.drive_id,
                item.item_id,
                item.name,
            )
            return False
        return path_passes_filter(
            item_path,
            include_paths=spec.include_paths,
            exclude_paths=spec.exclude_paths,
        )

    def _probe_include_paths(self) -> None:
        """Warn at startup for any include_path the drive doesn't actually contain.

        One Graph call per include_path per drive. Transient errors
        (network, Graph 5xx) get logged as warnings but never raise —
        connector init must succeed even if the source is briefly
        unavailable so the next tick can retry the drain.
        """
        for spec in self._drives:
            for path in spec.include_paths:
                try:
                    exists = self._graph.path_exists(spec.drive_id, path)
                except Exception as exc:
                    logger.warning(
                        "event=sharepoint_probe_error drive=%s path=%s error=%s "
                        "(connector init continues; next tick will retry)",
                        spec.drive_id,
                        path,
                        exc,
                    )
                    continue
                if not exists:
                    logger.warning(
                        "event=sharepoint_probe_missing_folder drive=%s path=%s. "
                        "fix: confirm the folder exists in SharePoint, or remove "
                        "the entry from include_paths. next: re-run "
                        "`kairix worker apply-config` after editing the YAML.",
                        spec.drive_id,
                        path,
                    )

    def _item_to_event(self, item: DriveItemRef, *, drive_id: str) -> ChangeEvent | None:
        """Translate one envelope to a typed :class:`ChangeEvent`.

        Folder rows are filtered upstream in :func:`_parse_delta_page`;
        items missing both an id and a tombstone flag are dropped here
        (Graph occasionally yields empty markers at sync boundaries).
        """
        if not item.item_id:
            return None
        modified_at = item.last_modified_at or _now_iso()
        if item.removed:
            return ChangeEvent(
                op="deleted",
                item_id=item.item_id,
                modified_at=modified_at,
                metadata={_META_SENSITIVITY_KEY: self._default_sensitivity, "drive_id": drive_id},
            )
        return ChangeEvent(
            op="created",
            item_id=item.item_id,
            modified_at=modified_at,
            metadata={
                _META_SENSITIVITY_KEY: self._default_sensitivity,
                "drive_id": drive_id,
                "name": item.name,
                "mime": item.mime or "",
            },
        )

    # ------------------------------------------------------------------
    # ADR-021 (Wave E.5) — per-source envelope metadata
    # ------------------------------------------------------------------

    def metadata_for(self, item_id: str) -> SourceMetadata:
        """Return cached SharePoint drive-item envelope metadata.

        ADR-021: surfaces ``lastModifiedDateTime`` as modified_at,
        ``createdBy.user.displayName`` (falling back to
        ``lastModifiedBy.user.displayName``) as author,
        ``parent_path`` segments as tags, and the ``web_url`` /
        ``drive_id`` / ``mime`` as properties.
        """
        item = self._cache.get(item_id)
        if item is None:
            return SourceMetadata()
        tags: tuple[str, ...] = ()
        if item.parent_path:
            tags = tuple(seg for seg in item.parent_path.split("/") if seg)
        properties: dict[str, str] = {}
        if item.name:
            properties["name"] = item.name
        if item.web_url:
            properties["web_url"] = item.web_url
        if item.drive_id:
            properties["drive_id"] = item.drive_id
        if item.mime:
            properties["mime"] = item.mime
        author = item.created_by or item.last_modified_by
        return SourceMetadata(
            modified_at=item.last_modified_at,
            created_at=item.created_at,
            author=author,
            tags=tags,
            properties=properties,
        )


_PATH_FILTER_DOCS_HINT = "next: see docs/architecture/sharepoint-path-filtering.md."

# F17 — error-message prefix repeated across path-list parse + overlap
# validation; extracted so the literal has one edit site.
_DRIVE_ERROR_PREFIX = "sharepoint: drive "


def path_passes_filter(
    item_path: str | None,
    *,
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
) -> bool:
    """Return True when the item's full path should be emitted.

    Segment-boundary prefix match — ``/Foo`` matches ``/Foo`` itself and
    ``/Foo/bar/baz.docx`` but NOT ``/Foo-Backup/...``. Case-insensitive
    (SharePoint paths are case-preserving but case-insensitive in API).

    Empty ``include_paths`` means "include everything". Non-empty
    ``include_paths`` means "include only items matching at least one
    entry". ``exclude_paths`` drops matches regardless of include —
    exclude wins.

    ``item_path`` of ``None`` (Graph envelope omitted parentReference.path)
    is treated as "no path known": included only when ``include_paths``
    is empty; otherwise dropped (we can't tell whether it matches, and an
    operator who set a strict scope clearly intended the boundary).
    Callers can emit a debug log on the drop so surprise misses are
    grep-able.
    """
    if not include_paths and not exclude_paths:
        return True
    if item_path is None:
        return not include_paths
    lowered = item_path.lower()
    if include_paths:
        if not any(_path_prefix_match(lowered, p.lower()) for p in include_paths):
            return False
    if exclude_paths:
        if any(_path_prefix_match(lowered, p.lower()) for p in exclude_paths):
            return False
    return True


def _path_prefix_match(item_path: str, candidate: str) -> bool:
    """Segment-boundary prefix match.

    ``/Foo`` matches the exact path ``/Foo`` and any descendant
    ``/Foo/bar/...`` but not the sibling ``/Foo-Backup/...``. Both inputs
    must already be lower-cased and the candidate must not have a
    trailing slash (the parser strips trailing slashes).
    """
    if item_path == candidate:
        return True
    return item_path.startswith(candidate + "/")


def _full_item_path(item: DriveItemRef) -> str | None:
    """Compose the operator-facing absolute path for a drive item.

    ``parent_path`` is the suffix after Graph's ``root:`` marker (e.g.
    ``/Curated-Content`` or ``/`` for items at the drive root); the
    item's ``name`` is the leaf. Returns ``None`` when the parent path
    was absent from the Graph envelope.
    """
    if item.parent_path is None:
        return None
    if item.parent_path in ("", "/"):
        return f"/{item.name}"
    return f"{item.parent_path}/{item.name}"


def _split_site_url(site_url: str) -> tuple[str | None, str | None]:
    """Split a friendly SharePoint site URL into ``(hostname, server_relative_path)``.

    ``https://contoso.sharepoint.com/sites/marketing`` →
    ``("contoso.sharepoint.com", "/sites/marketing")``. The components
    feed :meth:`SharePointGraphClient.resolve_site_by_path`. Tolerant of
    a missing scheme (``contoso.sharepoint.com/sites/marketing`` parses
    too). Returns ``(None, None)`` when the URL has no host or no
    server-relative path so the caller warns + skips that site.
    """
    trimmed = site_url.strip()
    if not trimmed:
        return None, None
    without_scheme = trimmed.split("://", 1)[-1]
    if "/" not in without_scheme:
        return None, None
    hostname, _, path = without_scheme.partition("/")
    if not hostname or not path:
        return None, None
    return hostname, "/" + path.strip("/")


def _serialise_cursor(per_drive: Mapping[str, str]) -> str:
    """Encode per-drive cursors as a deterministic JSON string."""
    return json.dumps(dict(per_drive), sort_keys=True, ensure_ascii=False)


def _deserialise_cursor(cursor: Cursor | None) -> dict[str, str]:
    """Decode the JSON-encoded per-drive cursor map.

    Tolerant of empty / malformed input — returns an empty dict so a
    cold-start tick (cursor=None) drives a full sweep without crashing
    on a stale legacy single-string cursor.
    """
    if not cursor:
        return {}
    try:
        parsed = json.loads(cursor)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items() if isinstance(v, str)}


def _parse_path_list(raw: object, field_name: str, drive_id: str) -> tuple[str, ...]:
    """Parse and validate an include_paths / exclude_paths list.

    Every entry must be a non-empty string starting with ``/``. Empty list
    or absent → empty tuple (no filtering). Raises with the standard
    fix-pointer shape on malformed input.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            _DRIVE_ERROR_PREFIX
            + f"{drive_id!r} {field_name} must be a list of path strings (got {type(raw).__name__}). "
            + f"fix: write {field_name} as a YAML list of strings starting with '/'. "
            + _PATH_FILTER_DOCS_HINT
        )
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise ValueError(
                _DRIVE_ERROR_PREFIX
                + f"{drive_id!r} {field_name} entry {entry!r} is not a non-empty string. "
                + f"fix: every {field_name} entry must be a non-empty string starting with '/'. "
                + _PATH_FILTER_DOCS_HINT
            )
        if not entry.startswith("/"):
            raise ValueError(
                _DRIVE_ERROR_PREFIX
                + f"{drive_id!r} {field_name} entry {entry!r} must start with '/'. "
                + "fix: prefix the path with a leading slash (e.g. '/Curated-Content'). "
                + _PATH_FILTER_DOCS_HINT
            )
        out.append(entry.rstrip("/") or "/")
    return tuple(out)


def _validate_no_exact_overlap(
    drive_id: str,
    include_paths: tuple[str, ...],
    exclude_paths: tuple[str, ...],
) -> None:
    """Refuse a config that has the exact same path in include and exclude.

    Strict children (e.g. include ``/Foo`` + exclude ``/Foo/draft``) are
    the intended use case and stay legal. Only exact equality triggers —
    that shape is almost always operator typo (copy-paste of a path into
    the wrong field) and refusing at parse time gives a fix-pointer
    instead of a silent "nothing indexed" outcome.
    """
    incl = {p.lower() for p in include_paths}
    excl = {p.lower() for p in exclude_paths}
    overlap = sorted(incl & excl)
    if overlap:
        raise ValueError(
            f"sharepoint: drive {drive_id!r} include_paths and exclude_paths both contain "
            f"the same path(s): {', '.join(repr(p) for p in overlap)}. "
            "fix: remove the duplicate from one of the lists, or split into separate "
            "connector instances if you wanted different sensitivity tiers per path. "
            "next: re-run `kairix config validate`. "
            "run: see docs/architecture/sharepoint-path-filtering.md."
        )


def _optional_str(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string, else ``None``.

    Narrows the ``object``-typed result of ``dict.get`` so the typed
    spec dataclasses receive a clean ``str | None``.
    """
    return value if isinstance(value, str) and value else None


# F17 — the include / exclude path-list field names are read by both the
# explicit-drive and site-discovery parsers; extracting them keeps the
# literal off the dup-string gate's radar (≥3 occurrences in a module).
_INCLUDE_PATHS_FIELD = "include_paths"
_EXCLUDE_PATHS_FIELD = "exclude_paths"


def _parse_include_exclude(entry: dict[str, object], label: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse + validate an entry's include/exclude path lists.

    Shared by the explicit-drive and site-discovery parsers. ``label``
    is the drive id (explicit) or a ``site:`` synthetic label (discovery)
    threaded into the fix-pointer error messages. Returns the
    ``(include_paths, exclude_paths)`` tuple after the no-exact-overlap
    validation.
    """
    include_paths = _parse_path_list(entry.get(_INCLUDE_PATHS_FIELD), _INCLUDE_PATHS_FIELD, label)
    exclude_paths = _parse_path_list(entry.get(_EXCLUDE_PATHS_FIELD), _EXCLUDE_PATHS_FIELD, label)
    _validate_no_exact_overlap(label, include_paths, exclude_paths)
    return include_paths, exclude_paths


def _parse_site_discovery_entry(entry: dict[str, object]) -> SiteDiscoverySpec:
    """Parse one config dict that names a SITE (no ``drive_id``).

    The caller has already established ``drive_id`` is absent and at
    least one of ``site_id`` / ``site_url`` is present. Inherited
    ``include_paths`` / ``exclude_paths`` apply to every discovered
    drive; the path-list parser reuses the drive validation surface with
    a synthetic label so the fix-pointer message still names the site.
    """
    site_id = _optional_str(entry.get("site_id"))
    site_url = _optional_str(entry.get("site_url"))
    label = f"site:{site_id or site_url}"
    include_paths, exclude_paths = _parse_include_exclude(entry, label)
    return SiteDiscoverySpec(
        site_id=site_id,
        site_url=site_url,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )


def _parse_explicit_drive_entry(entry: dict[str, object], drive_id: str) -> SharePointDriveSpec:
    """Parse one config dict that names an explicit ``drive_id``.

    Split out of :func:`parse_drive_entry` so the entry-shape routing
    (string / explicit-drive / site-discovery / reject) stays under
    F16's cognitive-complexity ceiling.
    """
    site_id = _optional_str(entry.get("site_id"))
    display = _optional_str(entry.get("display_name"))
    include_paths, exclude_paths = _parse_include_exclude(entry, drive_id)
    return SharePointDriveSpec(
        drive_id=drive_id,
        site_id=site_id,
        display_name=display,
        include_paths=include_paths,
        exclude_paths=exclude_paths,
    )


def parse_drive_entry(entry: object) -> SharePointDriveSpec | SiteDiscoverySpec:
    """Parse one operator-config drive entry into a typed spec.

    Three accepted shapes:

      * a bare ``drive_id`` string → :class:`SharePointDriveSpec`;
      * a mapping with ``drive_id`` (+ optional ``site_id`` /
        ``display_name`` / ``include_paths`` / ``exclude_paths``) →
        :class:`SharePointDriveSpec` (explicit drive, UNCHANGED
        behaviour); or
      * a mapping that names a SITE (``site_id`` and/or ``site_url``,
        NO ``drive_id``, + optional ``include_paths`` / ``exclude_paths``)
        → :class:`SiteDiscoverySpec` — "discover ALL drives in this
        site" (F42 self-discovery). Resolution happens lazily at sync
        time.

    A mapping carrying neither ``drive_id`` nor ``site_id``/``site_url``
    raises with an F21-style fix-pointer.

    Extracted from ``_drive_specs_from_config`` to keep that function
    under F16's cognitive-complexity ceiling — the per-entry isinstance
    branching pushed the parent function over 15.
    """
    if isinstance(entry, str) and entry:
        return SharePointDriveSpec(drive_id=entry)
    if isinstance(entry, dict):
        drive_id = entry.get("drive_id")
        if isinstance(drive_id, str) and drive_id:
            return _parse_explicit_drive_entry(entry, drive_id)
        if _optional_str(entry.get("site_id")) or _optional_str(entry.get("site_url")):
            return _parse_site_discovery_entry(entry)
        raise ValueError(
            "sharepoint: drive block names neither 'drive_id' nor a site. "
            "fix: declare an explicit drive_id (non-empty string), OR name a site "
            "via 'site_id' (and/or 'site_url') to auto-discover all of that site's drives. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8."
        )
    raise ValueError(
        f"sharepoint: drive entry {entry!r} is not a string or dict. "
        "fix: each drive entry must be a drive_id string, a block with drive_id, "
        "or a block naming a site to auto-discover. "
        "next: see docs/architecture/connector-ingestion-architecture.md §8."
    )


def _drive_specs_from_config(
    raw: object,
) -> tuple[tuple[SharePointDriveSpec, ...], tuple[SiteDiscoverySpec, ...]]:
    """Translate operator config drive entries to typed specs.

    Accepts a non-empty list whose entries are strings (treated as
    ``drive_id`` only), explicit-drive dicts (``drive_id`` + optional
    keys), or site-discovery dicts (``site_id``/``site_url`` + optional
    path filters). Returns a ``(explicit_drive_specs, site_specs)`` pair
    so the connector can sync explicit drives directly and expand site
    specs lazily at sync time. Anything else raises with a fix pointer so
    misconfigured operators see the contract surface loudly.
    """
    if not isinstance(raw, list) or not raw:
        raise ValueError(
            "sharepoint: 'drives' must be a non-empty list of drive ids or drive blocks. "
            "fix: declare at least one drive (drive_id) or site (site_id) under "
            "sharepoint -> drives in kairix.config.yaml. "
            "next: see docs/architecture/connector-ingestion-architecture.md §8 "
            "for the SharePoint connector config shape."
        )
    drive_specs: list[SharePointDriveSpec] = []
    site_specs: list[SiteDiscoverySpec] = []
    for entry in raw:
        parsed = parse_drive_entry(entry)
        if isinstance(parsed, SiteDiscoverySpec):
            site_specs.append(parsed)
        else:
            drive_specs.append(parsed)
    return tuple(drive_specs), tuple(site_specs)


def make_connector(config: Mapping[str, Any]) -> SharePointConnector:
    """Construct a :class:`SharePointConnector` from a config mapping.

    Expected keys:

      * ``drives`` (required) — non-empty list of drive specs. Each
        entry is either a drive-id string, a mapping with ``drive_id``
        plus optional ``site_id`` / ``display_name``, OR a mapping that
        names a SITE (``site_id`` and/or ``site_url``, no ``drive_id``)
        to auto-discover all of that site's drives at sync time (F42).
      * ``default_sensitivity`` (optional) — one of the F39 sensitivity
        literals; defaults to ``"internal"``.
      * ``max_delta_pages_per_tick`` (optional) — positive integer Graph-page
        cap for bounded first-sync backfills. Unlike the generic item-count
        pipeline budget, this stops only at Graph cursor boundaries.

    This factory stays PURE — no Graph call, no credential resolution
    at parse time. Site-discovery entries are parsed into typed specs
    here and expanded to concrete drives LAZILY on the first sync, when
    the connector already holds a live Graph client.

    Credentials resolve via :class:`kairix.secrets.loader.SecretsLoader`
    against the canonical identities ``(connector, m365, None, tenant-id)``,
    ``(connector, m365, None, client-id)``, and ``(connector, m365, None,
    client-secret)``. The loader's legacy-alias fallback resolves the
    historical ``CONNECTOR_M365_*`` / ``KAIRIX_M365_*`` / ``M365_*`` env
    vars transparently. The same canonical triple drives the M365
    email-headers + calendar siblings per ADR-019.

    Registered via ``[project.entry-points."kairix.connectors"]`` in
    kairix's ``pyproject.toml`` so the orchestration layer can resolve
    ``sharepoint`` to this factory by name.
    """
    drives, site_discovery = _drive_specs_from_config(config.get("drives"))
    declared = config.get("default_sensitivity", DEFAULT_SENSITIVITY)
    if declared not in ("public", "internal", "client-confidential", "personal"):
        raise ValueError(
            f"sharepoint: default_sensitivity {declared!r} is not a valid F39 tier. "
            "fix: set default_sensitivity to one of "
            "public / internal / client-confidential / personal. "
            "next: see kairix/core/protocols.py Sensitivity for the literal set."
        )
    sensitivity = cast(Sensitivity, declared)
    raw_page_budget = config.get("max_delta_pages_per_tick")
    if raw_page_budget is not None and (not isinstance(raw_page_budget, int) or raw_page_budget < 1):
        raise ValueError(
            f"sharepoint: max_delta_pages_per_tick {raw_page_budget!r} must be a positive integer. "
            "fix: set max_delta_pages_per_tick to an integer >= 1, or omit it to drain all pages. "
            "next: use this instead of per_tick_max_items for SharePoint backfill throttling."
        )
    return SharePointConnector(
        drives=list(drives),
        site_discovery=list(site_discovery),
        default_sensitivity=sensitivity,
        max_delta_pages_per_tick=raw_page_budget,
    )
