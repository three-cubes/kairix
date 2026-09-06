"""Thin httpx wrapper around Microsoft Graph for calendar event sync.

Wraps ``/users/<id>/calendar/calendarView`` (date-window filter) and
``/users/<id>/calendar/events/delta`` (delta token incremental sync).
The wrapper is deliberately narrow — it exposes one method per Graph
call the connector uses; chunking, signal extraction, and Bronze
persistence are upstream (per F35 / F38).

Per the architecture's three-layer split (docs/architecture/
provider-plugin-architecture.md, mirrored for connectors by F35), this
module ONLY imports from:

* :mod:`httpx` (third-party transport)
* :mod:`kairix.connectors.m365_calendar.auth` (the local OAuth2 helper)

It does NOT import from ``kairix.transport``, ``kairix.providers``,
``kairix.core.connectors``, or any sibling ``kairix.connectors.*``.

Per F42, the wrapper's public methods return frozen dataclasses
(:class:`CalendarEventRecord`) or tuples of them — never bare
``dict[str, Any]`` — so the connector's Protocol boundary is typed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Final

import httpx
from tenacity import (
    RetryCallState,
    RetryError,
    Retrying,
    retry_if_result,
    stop_after_attempt,
    wait_exponential,
)

from kairix.connectors.m365_calendar.auth import OAuth2ClientCredsAuth
from kairix.transport.errors import raise_for_graph_status

logger = logging.getLogger(__name__)

# Microsoft Graph base URL. The connector targets the v1.0 surface
# (calendarView + events/delta are both GA there). Beta surface is not
# used; if a future capability needs the beta endpoint it gets a new
# dedicated method, not a flag here.
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# Default page size for delta queries. Graph caps server-side at 50 for
# calendarView/delta; matching the cap keeps requests round-trip-
# efficient without tripping the cap-rejection error.
DEFAULT_PAGE_SIZE = 50

# Retry tuning for Graph throttling (GH #357). Mirrors the SharePoint
# Graph client's strategy: 429 + 503 honour ``Retry-After`` when present;
# other transient 5xx fall back to bounded exponential backoff. Graph
# documents 429 + 503 as the throttled responses; both carry
# ``Retry-After`` per https://learn.microsoft.com/graph/throttling.
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_BACKOFF_MIN_S: Final[float] = 2.0
_DEFAULT_BACKOFF_MAX_S: Final[float] = 60.0
_RETRY_AFTER_HEADER: Final[str] = "Retry-After"
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})
_THROTTLED_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 503})


@dataclass(frozen=True)
class CalendarEventRecord:
    """One calendar event as surfaced by Graph.

    Frozen-dataclass return type per F42. Carries the minimum fields
    the connector needs to emit a ``ChangeEvent`` + populate entity
    signals (attendees → Person/Org) downstream. The full Graph payload
    is preserved on :attr:`raw_payload` for Bronze persistence — Silver
    will pull additional fields out of there as needs evolve.

    ``cancelled`` distinguishes a Graph-level event cancellation
    (``isCancelled: true`` in the Graph payload) from an OData
    ``@removed`` tombstone — Silver maps both to a connector-level
    ``deleted`` :class:`ChangeEvent` so downstream timeline-update
    logic stays uniform.
    """

    event_id: str
    subject: str
    start_iso: str
    end_iso: str
    location: str
    attendees: tuple[str, ...]
    organiser: str
    last_modified_iso: str
    cancelled: bool
    removed: bool
    raw_payload: str


@dataclass(frozen=True)
class CalendarDeltaPage:
    """One page of a Graph delta query.

    ``events`` is the list of records on this page; ``next_link`` (if
    set) is the absolute URL of the next page; ``delta_link`` (if set)
    is the cursor to persist for the next sync tick. Exactly one of
    the two link fields is set on each Graph response per the OData
    delta-query contract.
    """

    events: tuple[CalendarEventRecord, ...]
    next_link: str | None
    delta_link: str | None


class M365GraphCalendarClient:
    """Narrow wrapper around the Graph calendar sync surface.

    Construction is cheap — no HTTP at __init__. The first
    :meth:`fetch_initial_delta` (or :meth:`fetch_delta_page`) call
    triggers the OAuth2 token exchange through
    :class:`OAuth2ClientCredsAuth`.

    DI seams:

    * ``http_client`` — :class:`httpx.Client` instance. Tests inject a
      pre-configured ``MockTransport`` so no real network I/O fires.
      Default is a fresh :class:`httpx.Client` bound to the OAuth2
      auth flow.
    * ``page_size`` — overrides the calendarView ``$top`` parameter.
    * ``sleep_fn`` — sleep shim used by the throttling-retry loop.
      Defaults to :func:`time.sleep`; tests pass a recording no-op so
      the suite stays fast without monkey-patching stdlib (GH #357).
    * ``max_attempts`` — total attempt count (initial call + retries)
      for any single Graph request. Defaults to
      :data:`_DEFAULT_MAX_ATTEMPTS` (3) per GH #357.
    """

    def __init__(
        self,
        user_id: str,
        auth: OAuth2ClientCredsAuth,
        http_client: httpx.Client | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        sleep_fn: Callable[[float], None] = time.sleep,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._user_id = user_id
        self._auth = auth
        self._http = http_client or httpx.Client(auth=auth, timeout=60.0)
        self._page_size = page_size
        self._sleep_fn = sleep_fn
        self._max_attempts = max_attempts

    def fetch_initial_delta(self, start_iso: str, end_iso: str) -> CalendarDeltaPage:
        """First-time sync — pull a date-window of events with no cursor.

        Returns one page of :class:`CalendarEventRecord` plus a
        ``next_link`` / ``delta_link`` for follow-up. The orchestrator
        drains all pages by calling :meth:`fetch_delta_page` in a loop
        until ``delta_link`` is set.

        ``start_iso`` and ``end_iso`` must be ISO-8601 UTC timestamps.
        The connector picks the window (default: 90 days back, 270
        days forward; see :func:`kairix.connectors.m365_calendar.connector.default_window`).

        Page size is expressed via the ``Prefer: odata.maxpagesize=N``
        header — Graph's ``calendarView/delta`` rejects the ``$top``
        query parameter on this resource with
        ``ErrorInvalidUrlQuery`` (#382 part 3).
        """
        path = f"/users/{self._user_id}/calendar/calendarView/delta"
        params: dict[str, Any] = {
            "startDateTime": start_iso,
            "endDateTime": end_iso,
        }
        headers = {"Prefer": f"odata.maxpagesize={self._page_size}"}
        response = self._retrying_get(f"{GRAPH_BASE_URL}{path}", params=params, headers=headers)
        return _parse_delta_response(response.json())

    def fetch_delta_page(self, link: str) -> CalendarDeltaPage:
        """Follow a Graph-returned ``@odata.nextLink`` / ``@odata.deltaLink``.

        Graph encodes the cursor state into the URL — the orchestrator
        passes the exact link back unchanged. The auth flow re-injects
        the Bearer token on every request, so the same client + auth
        wraps both initial and incremental pages.
        """
        response = self._retrying_get(link)
        return _parse_delta_response(response.json())

    # ------------------------------------------------------------------
    # Internals — throttle-aware retry loop (GH #357)
    # ------------------------------------------------------------------

    def _retrying_get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Issue an authorised GET with retry-with-backoff on throttle.

        Mirrors :class:`SharePointGraphClient._authorised_get` (GH #357):

          * **429 / 5xx with backoff.** Throttled (429) and Service
            Unavailable (503) responses honour the server's
            ``Retry-After`` header; other 5xx (500, 502, 504) fall back
            to bounded exponential backoff. ``max_attempts`` total
            attempts (initial call + retries). After exhaustion the
            final response is returned to ``raise_for_status`` which
            converts it to :class:`httpx.HTTPStatusError`.
          * **Other 4xx (including 401, 403, 404).** Raise immediately —
            permanent for this URL + credential pair. The 401 case is
            currently NOT given a single-retry path here (unlike the
            email-headers / SharePoint clients) because the calendar
            client's :class:`OAuth2ClientCredsAuth` is wired as an
            :class:`httpx.Auth` that re-injects the bearer on every
            request; an expired token surfaces through the auth flow
            itself, not through a 401 from this layer.
        """
        retrying = Retrying(
            retry=retry_if_result(_is_retryable_response),
            wait=self._wait_strategy,
            stop=stop_after_attempt(self._max_attempts),
            sleep=self._sleep_fn,
            reraise=True,
        )
        try:
            response = retrying(self._do_get, url, params, headers)
        except RetryError as exc:
            # ``retry_if_result`` returns a "successful" outcome from
            # tenacity's perspective so ``reraise=True`` can't lift an
            # exception; on stop-condition exhaustion the final attempt's
            # response is wrapped in :class:`RetryError`. Lift it and
            # convert via ``raise_for_status`` so callers see the same
            # :class:`httpx.HTTPStatusError` shape they did before retry
            # was added.
            final: httpx.Response = exc.last_attempt.result()
            raise_for_graph_status(final)
            return final  # pragma: no cover — raise_for_status above always raises here
        raise_for_graph_status(response)
        return response

    def _do_get(
        self,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Single HTTP GET. The bearer is injected by the
        :class:`httpx.Auth` flow attached to the underlying client.
        """
        kwargs: dict[str, Any] = {}
        if params is not None:
            kwargs["params"] = params
        if headers is not None:
            kwargs["headers"] = headers
        return self._http.get(url, **kwargs)

    def _wait_strategy(self, retry_state: RetryCallState) -> float:
        """Compute the wait between retries (GH #357).

        For 429 / 503 responses honour the server's ``Retry-After`` header
        (seconds). For other retryable statuses (or when ``Retry-After``
        is missing / unparseable) fall back to exponential backoff
        between :data:`_DEFAULT_BACKOFF_MIN_S` and
        :data:`_DEFAULT_BACKOFF_MAX_S`.
        """
        outcome = retry_state.outcome
        if outcome is None or outcome.failed:  # pragma: no cover — exception path bypasses retry_if_result
            return _DEFAULT_BACKOFF_MIN_S
        response = outcome.result()
        retry_after = _parse_retry_after(response) if response.status_code in _THROTTLED_STATUS_CODES else None
        if retry_after is not None:
            logger.warning(
                "m365 calendar graph: %s on attempt %d; honouring Retry-After=%.1fs",
                response.status_code,
                retry_state.attempt_number,
                retry_after,
            )
            return retry_after
        backoff = wait_exponential(multiplier=1, min=_DEFAULT_BACKOFF_MIN_S, max=_DEFAULT_BACKOFF_MAX_S)(retry_state)
        logger.warning(
            "m365 calendar graph: %s on attempt %d; backing off %.1fs",
            response.status_code,
            retry_state.attempt_number,
            backoff,
        )
        return backoff

    def close(self) -> None:
        """Close the underlying :class:`httpx.Client`."""
        self._http.close()

    def __enter__(self) -> M365GraphCalendarClient:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def _is_retryable_response(response: httpx.Response) -> bool:
    """``True`` when ``response.status_code`` is in
    :data:`_RETRYABLE_STATUS_CODES` (429 + 5xx subset).

    Used by the :class:`Retrying` loop in
    :meth:`M365GraphCalendarClient._retrying_get` to decide whether the
    request gets retried (with the wait dictated by
    :meth:`M365GraphCalendarClient._wait_strategy`) or returned to the
    caller for ``raise_for_status``.
    """
    return response.status_code in _RETRYABLE_STATUS_CODES


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Return the ``Retry-After`` header (seconds) as a float, or ``None``.

    Graph emits ``Retry-After`` as an integer second count per
    https://learn.microsoft.com/graph/throttling. The HTTP spec also
    allows an HTTP-date form; this client only parses the seconds form
    and falls back to exponential backoff for anything unparseable.
    """
    raw = response.headers.get(_RETRY_AFTER_HEADER)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_delta_response(payload: dict[str, Any]) -> CalendarDeltaPage:
    """Translate a Graph JSON payload to a typed :class:`CalendarDeltaPage`.

    Graph's calendarView/delta payload is an OData envelope with::

        {
          "@odata.context": "...",
          "@odata.nextLink": "..." | absent,
          "@odata.deltaLink": "..." | absent,
          "value": [ { event payload }, ... ]
        }

    Tombstoned events carry ``@removed`` instead of a full event body;
    they're surfaced as :attr:`CalendarEventRecord.removed=True`.
    """
    records: list[CalendarEventRecord] = []
    for item in payload.get("value", []):
        records.append(_record_from_graph_event(item))

    next_link_raw = payload.get("@odata.nextLink")
    delta_link_raw = payload.get("@odata.deltaLink")
    next_link = str(next_link_raw) if isinstance(next_link_raw, str) else None
    delta_link = str(delta_link_raw) if isinstance(delta_link_raw, str) else None
    return CalendarDeltaPage(
        events=tuple(records),
        next_link=next_link,
        delta_link=delta_link,
    )


def _record_from_graph_event(item: dict[str, Any]) -> CalendarEventRecord:
    """Build a :class:`CalendarEventRecord` from one Graph event JSON object."""
    if "@removed" in item:
        return CalendarEventRecord(
            event_id=str(item.get("id", "")),
            subject="",
            start_iso="",
            end_iso="",
            location="",
            attendees=(),
            organiser="",
            last_modified_iso="",
            cancelled=False,
            removed=True,
            raw_payload="",
        )

    attendees = _attendee_emails(item.get("attendees", []))
    organiser = _organiser_email(item.get("organizer", {}))
    location_value = _location_display(item.get("location", {}))
    start = _datetime_iso(item.get("start", {}))
    end = _datetime_iso(item.get("end", {}))
    cancelled = bool(item.get("isCancelled", False))
    subject = str(item.get("subject", "") or "")
    last_modified = str(item.get("lastModifiedDateTime", "") or "")

    return CalendarEventRecord(
        event_id=str(item.get("id", "")),
        subject=subject,
        start_iso=start,
        end_iso=end,
        location=location_value,
        attendees=attendees,
        organiser=organiser,
        last_modified_iso=last_modified,
        cancelled=cancelled,
        removed=False,
        raw_payload=str(item),
    )


def _attendee_emails(raw: list[dict[str, Any]] | Any) -> tuple[str, ...]:
    """Pull attendee email addresses out of the Graph ``attendees`` array."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        email_obj = entry.get("emailAddress", {})
        if not isinstance(email_obj, dict):
            continue
        addr = email_obj.get("address")
        if isinstance(addr, str) and addr:
            out.append(addr)
    return tuple(out)


def _organiser_email(raw: dict[str, Any]) -> str:
    """Pull the organiser email out of the Graph ``organizer`` object."""
    if not isinstance(raw, dict):
        return ""
    email_obj = raw.get("emailAddress", {})
    if not isinstance(email_obj, dict):
        return ""
    addr = email_obj.get("address", "")
    return str(addr or "")


def _location_display(raw: dict[str, Any]) -> str:
    """Pull the display name out of the Graph ``location`` object."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("displayName", "") or "")


def _datetime_iso(raw: dict[str, Any]) -> str:
    """Pull the ISO-8601 timestamp out of a Graph ``{dateTime, timeZone}`` object."""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("dateTime", "") or "")


def iter_pages(client: M365GraphCalendarClient, first_page: CalendarDeltaPage) -> Iterator[CalendarDeltaPage]:
    """Drain pages starting from ``first_page`` until a ``delta_link`` arrives.

    Helper that exposes the OData ``@odata.nextLink`` walk as a Python
    iterator. The connector uses this to assemble the full set of
    events seen in one sync tick. The final yielded page carries the
    ``delta_link`` to persist as the next cursor.
    """
    yield first_page
    page = first_page
    while page.next_link is not None:
        page = client.fetch_delta_page(page.next_link)
        yield page
