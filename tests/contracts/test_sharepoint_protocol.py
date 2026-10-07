"""Contract test for the SharePoint connector plugin (F43).

Exercises the canonical fake
(:class:`tests.fakes.FakeSharePointConnector`) AND the real
implementation
(:class:`kairix.connectors.sharepoint.SharePointConnector`) through
the same :class:`~kairix.core.protocols.SourceConnector` Protocol
assertions. F43 requires this pairing — without it the fake can
drift from the real wire (or vice versa) and the production path
silently diverges from what BDD / unit tests measure.

Real-impl path is driven against an :class:`httpx.MockTransport`-backed
Graph stub; no real network call is ever made.

Sabotage proofs:

  * Removing ``list_changes`` from
    :class:`SharePointConnector` flips
    ``test_connector_satisfies_source_connector_protocol`` (real branch)
    to False.
  * Replacing the connector's ``fetch`` return shape with a plain
    ``bytes`` value (skipping the :class:`RawArtefact` wrapper) breaks
    ``test_connector_fetch_returns_binary_artefact``.
  * Mutating :data:`DEFAULT_SENSITIVITY` to ``"public"`` flips
    ``test_connector_default_sensitivity_is_internal``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from kairix.connectors.sharepoint import (
    DEFAULT_SENSITIVITY,
    SharePointConnector,
    SharePointCredentials,
    SharePointDriveSpec,
    SharePointGraphClient,
)
from kairix.core.protocols import ChangeEvent, RawArtefact, SourceConnector
from kairix.transport.auth.oauth2_client_creds import OAuth2ClientCredsAuth
from tests.fakes import FakeSharePointConnector

pytestmark = pytest.mark.contract

_DRIVE_ID = "b!drive-contract"
_DELTA_LINK = f"https://graph.microsoft.com/v1.0/drives/{_DRIVE_ID}/root/delta?$deltatoken=contract"


def _envelope_items() -> list[dict[str, Any]]:
    """Two seeded envelopes that round-trip through both branches."""
    return [
        {
            "id": "01ITEMALPHA",
            "name": "alpha.pdf",
            "mimeType": "application/pdf",
            "lastModifiedDateTime": "2026-05-22T10:00:00Z",
            "webUrl": "https://contoso.sharepoint.com/sites/team/Documents/alpha.pdf",
            "driveId": _DRIVE_ID,
            "_content": b"%PDF-1.4 fake pdf contract content",
        },
        {
            "id": "01ITEMBRAVO",
            "name": "bravo.docx",
            "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "lastModifiedDateTime": "2026-05-22T11:00:00Z",
            "webUrl": "https://contoso.sharepoint.com/sites/team/Documents/bravo.docx",
            "driveId": _DRIVE_ID,
            "_content": b"PK\x03\x04 fake docx contract content",
        },
    ]


def _fake_factory() -> SourceConnector:
    """Canonical fake factory — seeds two envelopes."""
    return FakeSharePointConnector(items=_envelope_items(), delta_link=_DELTA_LINK)


def _delta_page_payload() -> dict[str, Any]:
    return {
        "@odata.context": f"https://graph.microsoft.com/v1.0/$metadata#drives/{_DRIVE_ID}/root/delta",
        "value": [
            {
                "id": entry["id"],
                "name": entry["name"],
                "lastModifiedDateTime": entry["lastModifiedDateTime"],
                "webUrl": entry["webUrl"],
                "file": {"mimeType": entry["mimeType"]},
                "parentReference": {"driveId": entry["driveId"]},
                "size": len(entry["_content"]),
            }
            for entry in _envelope_items()
        ],
        "@odata.deltaLink": _DELTA_LINK,
    }


def _real_factory() -> SourceConnector:
    """Real-impl factory — MockTransport-backed Graph stub."""

    def _stub(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(200, json={"access_token": "fake-bearer", "expires_in": 3600, "token_type": "Bearer"})
        if "/content" in url:
            for entry in _envelope_items():
                if entry["id"] in url:
                    return httpx.Response(200, content=entry["_content"])
            return httpx.Response(404)
        return httpx.Response(200, json=_delta_page_payload())

    shared = httpx.Client(transport=httpx.MockTransport(_stub))
    auth = OAuth2ClientCredsAuth(
        tenant_id="fake-tenant",
        client_id="fake-client",
        client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    connector = SharePointConnector(
        drives=[SharePointDriveSpec(drive_id=_DRIVE_ID)],
        credentials=SharePointCredentials(
            tenant_id="fake-tenant",
            client_id="fake-client",
            client_secret="fake-secret-value",  # pragma: allowlist secret — test fixture
        ),
        auth=auth,
        client_builder=lambda a: SharePointGraphClient(auth=a, http_client=shared),
    )
    # Prime the envelope cache so fetch() works in the contract assertions
    # (same shape as the M365 email-headers contract test).
    list(connector.list_changes(cursor=None))
    return connector


_FACTORIES: list[tuple[str, Callable[[], SourceConnector]]] = [
    ("fake", _fake_factory),
    ("real", _real_factory),
]


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_satisfies_source_connector_protocol(name: str, factory: Callable[[], SourceConnector]) -> None:
    """F43: both fake and real impl satisfy the runtime-checkable Protocol."""
    connector = factory()
    assert isinstance(connector, SourceConnector), f"{name!r} factory output is not a SourceConnector"
    assert connector.name == "sharepoint"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_list_changes_returns_change_events(name: str, factory: Callable[[], SourceConnector]) -> None:
    """Both implementations stream :class:`ChangeEvent` instances."""
    connector = factory()
    events = list(connector.list_changes(cursor=None))
    assert events, f"{name!r} factory produced no events"
    for ev in events:
        assert isinstance(ev, ChangeEvent), f"{name!r} yielded a non-ChangeEvent: {ev!r}"
        assert ev.op in ("created", "modified", "deleted")


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_fetch_returns_binary_artefact(name: str, factory: Callable[[], SourceConnector]) -> None:
    """Both implementations satisfy the ``fetch`` -> :class:`RawArtefact` shape."""
    connector = factory()
    artefact = connector.fetch("01ITEMALPHA")
    assert isinstance(artefact, RawArtefact), f"{name!r} fetch did not return a RawArtefact: {artefact!r}"
    assert artefact.mime == "application/pdf", f"{name!r} fetch mime is wrong: {artefact.mime!r}"
    assert artefact.raw, f"{name!r} fetch raw bytes is empty"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_source_link_round_trips_to_sharepoint(name: str, factory: Callable[[], SourceConnector]) -> None:
    """``source_link`` returns a SharePoint or sharepoint:// URL on both impls."""
    connector = factory()
    link = connector.source_link("01ITEMALPHA")
    assert link, f"{name!r} produced empty source_link"
    assert link.startswith(("https://", "sharepoint://")), f"{name!r} unexpected link scheme: {link!r}"


@pytest.mark.contract
@pytest.mark.parametrize("name,factory", _FACTORIES)
def test_connector_default_sensitivity_is_internal(name: str, factory: Callable[[], SourceConnector]) -> None:
    """``sensitivity_for`` returns the documented default ``internal`` tier."""
    connector = factory()
    tier = connector.sensitivity_for("01ITEMALPHA")
    assert tier == DEFAULT_SENSITIVITY == "internal", f"{name!r} returned unexpected sensitivity: {tier!r}"


# ---------------------------------------------------------------------------
# Path filtering — Protocol surface holds with filter active
# ---------------------------------------------------------------------------


def _filtered_real_factory() -> SourceConnector:
    """Real connector with ``include_paths`` active over a one-envelope Graph stub."""
    transport = httpx.MockTransport(_handler_returning_one_envelope())
    shared = httpx.Client(transport=transport)
    auth = OAuth2ClientCredsAuth(
        tenant_id="t",
        client_id="c",
        client_secret="s-value",  # pragma: allowlist secret — contract test fixture
        scope="https://graph.microsoft.com/.default",
        http_client=shared,
    )
    return SharePointConnector(
        drives=[SharePointDriveSpec(drive_id=_DRIVE_ID, include_paths=("/Curated-Content",))],
        credentials=SharePointCredentials(
            tenant_id="t",
            client_id="c",
            client_secret="s-value",  # pragma: allowlist secret — contract test fixture
        ),
        auth=auth,
        client_builder=lambda a: SharePointGraphClient(auth=a, http_client=shared),
    )


def _filtered_fake_factory() -> SourceConnector:
    """Fake seeded with the one in-scope envelope the filtered real connector keeps."""
    return FakeSharePointConnector(
        items=[
            {
                "id": "contract-item",
                "name": "doc.md",
                "mimeType": "text/markdown",
                "lastModifiedDateTime": "2026-05-22T10:00:00Z",
                "driveId": _DRIVE_ID,
            }
        ],
        delta_link=_DELTA_LINK,
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "factory",
    [_filtered_real_factory, _filtered_fake_factory],
    ids=["real", "fake"],
)
def test_real_connector_with_path_filter_still_satisfies_protocol(factory: Callable[[], SourceConnector]) -> None:
    """SharePointConnector with include_paths set still satisfies the
    SourceConnector Protocol shape — the filter is pure post-processing,
    no Protocol surface changes.

    The fake leg is seeded with exactly the in-scope envelope (the fake
    does not model filtering — a real-connector implementation detail);
    both legs must stream the same well-formed ChangeEvents.

    Sabotage proof: in ``SharePointConnector._item_passes_spec_filter``
    return ``False`` unconditionally → the real leg yields no events and
    the item-id assertion fails → restored.
    """
    connector = factory()
    # Same Protocol assertions as the no-filter case — none of these
    # depend on whether items get filtered.
    assert isinstance(connector, SourceConnector)
    events = list(connector.list_changes(cursor=None))
    assert [e.item_id for e in events] == ["contract-item"]
    for e in events:
        assert isinstance(e.item_id, str) and e.item_id
        assert isinstance(e.op, str) and e.op in ("created", "modified", "deleted")
        assert isinstance(e.modified_at, str) and e.modified_at


def _handler_returning_one_envelope():
    """Stub Graph endpoint that returns one envelope in /Curated-Content."""
    body = {
        "@odata.context": f"https://graph.microsoft.com/v1.0/$metadata#drives/{_DRIVE_ID}/root/delta",
        "value": [
            {
                "id": "contract-item",
                "name": "doc.md",
                "size": 50,
                "lastModifiedDateTime": "2026-05-22T10:00:00Z",
                "webUrl": "https://contoso.sharepoint.com/sites/team/Documents/Curated-Content/doc.md",
                "file": {"mimeType": "text/markdown"},
                "parentReference": {
                    "driveId": _DRIVE_ID,
                    "path": f"/drives/{_DRIVE_ID}/root:/Curated-Content",
                },
            }
        ],
        "@odata.deltaLink": _DELTA_LINK,
    }

    def handler(request):
        url = str(request.url)
        if "/oauth2/v2.0/token" in url:
            return httpx.Response(
                200,
                json={"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"},
            )
        if "/root:" in url and "delta" not in url:
            return httpx.Response(200, json={"id": "folder-id"})
        return httpx.Response(200, json=body)

    return handler
