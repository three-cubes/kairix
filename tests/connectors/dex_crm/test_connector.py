"""Unit tests for :class:`kairix.connectors.dex_crm.DexCrmConnector`.

Scope per the KP-1 brief:

  * Three configured Dex API listings (contacts / organisations /
    relationships) — list_changes(None) emits one ``modified`` event
    per record across all three endpoints.
  * Cursor filter — records older than the cursor are skipped.
  * Pagination — the iter_listing loop walks through ``next_cursor``.
  * fetch round-trips the cached record bytes.
  * source_link routes by record kind.
  * Missing secret — first list_changes call raises
    :class:`MissingCredentialsError` with a ``fix:`` message.
  * Rate limit (429) — exponential backoff retries up to the
    configured cap.

All tests drive an in-process :class:`httpx.MockTransport` so the suite
never reaches the public Dex API. The auth seam is either a subclassed
:class:`ApiKeyAuth` (happy_path) or the production
:class:`ApiKeyAuth` paired with the real :func:`get_secret` chain via
a per-test env clear (missing-credentials path).

Sabotage proofs (executed during development):
  * Mutating :meth:`DexCrmConnector._normalise` to always return ``None``
    drops every record — :func:`test_list_changes_emits_one_event_per_record`
    flunks at the ``assert events`` line. Restored.
  * Removing the cursor filter in :meth:`DexCrmConnector.list_changes`
    means the cursor-based filter test sees too many events — fails.
    Restored.

F1-clean (no monkey-patching of kairix internals), F8 carries
``@pytest.mark.unit``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from kairix.connectors.dex_crm import DexCrmConnector, make_connector
from kairix.connectors.dex_crm.client import DexCrmClient, DexCrmClientConfig
from kairix.connectors.dex_crm.connector import CONNECTOR_NAME
from kairix.core.protocols import ChangeEvent, RawArtefact, Sensitivity
from kairix.transport.auth.api_key import (
    ApiKeyAuth,
    BearerHeaders,
    MissingCredentialsError,
    reset_api_key_cache,
)

pytestmark = pytest.mark.unit


def _stub_auth(token: str = "dex-unit-token") -> ApiKeyAuth:
    """Subclass-based auth override — F1-clean."""

    class _ScriptedAuth(ApiKeyAuth):
        def headers(self, _secret_name: str) -> BearerHeaders:
            return BearerHeaders(mapping={"Authorization": f"Bearer {token}"})

    return _ScriptedAuth()


def _build_connector(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sensitivity: Sensitivity = "internal",
) -> DexCrmConnector:
    """Construct the connector with a recording transport."""
    inner = httpx.Client(transport=httpx.MockTransport(handler))
    client = DexCrmClient(
        config=DexCrmClientConfig(rate_limit_sleep_s=0.0, max_retries=3, backoff_base_s=0.001),
        http_client=inner,
        auth=_stub_auth(),
        sleep=lambda _s: None,
    )
    return DexCrmConnector(client=client, sensitivity=sensitivity)


# ---------------------------------------------------------------------------
# Happy-path: three listings, one record each, three ChangeEvents.
# ---------------------------------------------------------------------------


def _three_listings_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/contacts"):
        return httpx.Response(
            200,
            json={
                "data": [{"id": "c-1", "updated_at": "2026-05-22T01:00:00Z"}],
                "next_cursor": None,
            },
        )
    if request.url.path.endswith("/organisations"):
        return httpx.Response(
            200,
            json={
                "data": [{"id": "o-1", "updated_at": "2026-05-22T02:00:00Z"}],
                "next_cursor": None,
            },
        )
    if request.url.path.endswith("/relationships"):
        return httpx.Response(
            200,
            json={
                "data": [{"id": "r-1", "updated_at": "2026-05-22T03:00:00Z"}],
                "next_cursor": None,
            },
        )
    return httpx.Response(404, json={"error": f"unexpected path: {request.url.path}"})


def test_list_changes_emits_one_event_per_record() -> None:
    """Three configured listings → three ChangeEvents in order."""
    connector = _build_connector(_three_listings_handler)
    events = list(connector.list_changes(cursor=None))
    assert len(events) == 3, f"expected 3 events; got {events!r}"
    item_ids = [ev.item_id for ev in events]
    assert item_ids == ["contact:c-1", "organisation:o-1", "relationship:r-1"]
    for ev in events:
        assert isinstance(ev, ChangeEvent)
        assert ev.op == "modified"
        assert ev.modified_at.endswith("Z")


def test_connector_exposes_canonical_name() -> None:
    """``DexCrmConnector.name`` is the entry-point key the registry uses."""
    connector = _build_connector(_three_listings_handler)
    assert connector.name == CONNECTOR_NAME == "dex_crm"


def test_fetch_returns_json_artefact_for_listed_item() -> None:
    """``fetch`` returns the cached record bytes after a ``list_changes``."""
    connector = _build_connector(_three_listings_handler)
    list(connector.list_changes(cursor=None))
    artefact = connector.fetch("contact:c-1")
    assert isinstance(artefact, RawArtefact)
    assert artefact.mime == "application/json"
    assert b"c-1" in artefact.raw


def test_fetch_unknown_item_raises_keyerror() -> None:
    """An item_id never seen via list_changes raises ``KeyError`` with a fix hint."""
    connector = _build_connector(_three_listings_handler)
    with pytest.raises(KeyError, match=r"fix:"):
        connector.fetch("contact:never-seen")


def test_source_link_routes_by_record_kind() -> None:
    """Each kind routes to its Dex UI path."""
    connector = _build_connector(_three_listings_handler)
    assert connector.source_link("contact:c-1").startswith("https://app.getdex.com/contacts/")
    assert connector.source_link("organisation:o-1").startswith("https://app.getdex.com/organisations/")
    assert connector.source_link("relationship:r-1").startswith("https://app.getdex.com/relationships/")
    # An item_id without a kind prefix routes to contacts (backward-compat).
    assert connector.source_link("c-1").startswith("https://app.getdex.com/contacts/")


def test_sensitivity_for_returns_configured_tier() -> None:
    """Operator-configured sensitivity overrides the default ``internal``."""
    connector = _build_connector(_three_listings_handler, sensitivity="client-confidential")
    assert connector.sensitivity_for("contact:c-1") == "client-confidential"


# ---------------------------------------------------------------------------
# Cursor filtering.
# ---------------------------------------------------------------------------


def test_cursor_filters_records_older_than_cursor() -> None:
    """Records with ``updated_at <= cursor`` are skipped."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contacts"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "c-old", "updated_at": "2026-05-22T01:00:00Z"},
                        {"id": "c-new", "updated_at": "2026-05-22T05:00:00Z"},
                    ],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    connector = _build_connector(_handler)
    events = list(connector.list_changes(cursor="2026-05-22T02:00:00Z"))
    item_ids = [ev.item_id for ev in events]
    assert item_ids == ["contact:c-new"], f"cursor should filter c-old; got {item_ids!r}"


# ---------------------------------------------------------------------------
# Pagination.
# ---------------------------------------------------------------------------


def test_pagination_walks_next_cursor() -> None:
    """The client follows ``next_cursor`` until exhaustion."""
    state = {"contacts_page": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contacts"):
            state["contacts_page"] += 1
            if state["contacts_page"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "data": [{"id": "c-1", "updated_at": "2026-05-22T01:00:00Z"}],
                        "next_cursor": "page-2-token",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "c-2", "updated_at": "2026-05-22T02:00:00Z"}],
                    "next_cursor": None,
                },
            )
        return httpx.Response(200, json={"data": [], "next_cursor": None})

    connector = _build_connector(_handler)
    events = list(connector.list_changes(cursor=None))
    contact_ids = [ev.item_id for ev in events if ev.item_id.startswith("contact:")]
    assert contact_ids == ["contact:c-1", "contact:c-2"], f"pagination broken; got {contact_ids!r}"
    assert state["contacts_page"] == 2, "client must follow next_cursor to page 2"


# ---------------------------------------------------------------------------
# Rate-limit retry.
# ---------------------------------------------------------------------------


def test_rate_limit_retries_with_backoff() -> None:
    """A 429 is retried with exponential backoff before succeeding."""
    state = {"attempts": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/contacts"):
            return httpx.Response(200, json={"data": [], "next_cursor": None})
        state["attempts"] += 1
        if state["attempts"] < 3:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json={
                "data": [{"id": "c-1", "updated_at": "2026-05-22T01:00:00Z"}],
                "next_cursor": None,
            },
        )

    connector = _build_connector(_handler)
    events = list(connector.list_changes(cursor=None))
    assert state["attempts"] == 3, f"client must retry 429; attempts={state['attempts']}"
    contact_events = [ev for ev in events if ev.item_id.startswith("contact:")]
    assert contact_events, "retry must succeed after backoff"


# ---------------------------------------------------------------------------
# Missing-credentials surface.
# ---------------------------------------------------------------------------


def test_missing_secret_raises_typed_error_with_fix_hint() -> None:
    """When the secret is unset, first ``list_changes`` raises a typed error.

    The connector still constructs OK at this point — only the
    operational call raises. The unset secret is expressed through
    ``ApiKeyAuth``'s ``secret_lookup`` seam (a resolver that finds
    nothing), not by deleting ``KAIRIX_SECRETS_*`` / ``KAIRIX_KV_NAME``
    from the process env (F2) — so the outcome cannot depend on what
    the developer's shell happens to export.

    Sabotage proof (executed): make ``ApiKeyAuth.headers`` ignore an
    empty resolution (drop the ``resolved is None`` raise) and the
    ``pytest.raises(MissingCredentialsError)`` below fails; restored.
    """
    reset_api_key_cache()

    inner = httpx.Client(transport=httpx.MockTransport(_three_listings_handler))
    client = DexCrmClient(
        config=DexCrmClientConfig(rate_limit_sleep_s=0.0),
        http_client=inner,
        auth=ApiKeyAuth(secret_lookup=lambda _secret_name: None),
        sleep=lambda _s: None,
    )
    connector = DexCrmConnector(client=client)

    with pytest.raises(MissingCredentialsError, match=r"fix:"):
        list(connector.list_changes(cursor=None))


# ---------------------------------------------------------------------------
# Factory shape.
# ---------------------------------------------------------------------------


def test_make_connector_returns_dex_connector_with_overrides() -> None:
    """The entry-point factory honours operator config overrides."""
    connector = make_connector(
        {
            "base_url": "https://example.invalid/v1",
            "secret_name": "alternative-secret-name",  # pragma: allowlist secret
            "page_size": 25,
            "sensitivity": "client-confidential",
            "rate_limit_sleep_s": 0.0,
        }
    )
    assert isinstance(connector, DexCrmConnector)
    assert connector.sensitivity_for("contact:c-1") == "client-confidential"


def test_make_connector_uses_defaults_for_minimal_config() -> None:
    """An empty config still builds a usable connector — defaults populate."""
    connector = make_connector({})
    assert isinstance(connector, DexCrmConnector)
    assert connector.sensitivity_for("contact:c-1") == "internal"
