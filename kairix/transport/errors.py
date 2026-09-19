"""Typed failures shared by external HTTP connector clients."""

from __future__ import annotations

import httpx


class GraphDeltaExpiredError(httpx.HTTPStatusError):
    """Microsoft Graph rejected an expired delta cursor with HTTP 410."""


def raise_for_graph_status(response: httpx.Response) -> None:
    """Raise a typed error for 410 and the normal httpx error otherwise."""
    if response.status_code == 410:
        raise GraphDeltaExpiredError(
            "Microsoft Graph delta cursor expired (410 Gone)",
            request=response.request,
            response=response,
        )
    response.raise_for_status()
