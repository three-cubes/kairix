"""Step definitions for embed_pool_config.feature.

Drives ``kairix.credentials.make_openai_client`` with explicit pool
kwargs and asserts on the resulting httpx pool. No env-monkeypatch (F2 —
the env-read mechanism is unit-tested separately in tests/test_paths.py;
this layer pins the kwarg-to-wire wiring).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import parsers, then, when

from kairix.credentials import make_openai_client

pytestmark = pytest.mark.bdd

_FOUNDRY_ENDPOINT = "https://example.services.ai.azure.com"


@pytest.fixture
def _pool_state() -> dict[str, Any]:
    """Per-scenario state container — accumulates kwargs across Given steps."""
    return {"kwargs": {}, "client": None}


# ---------------------------------------------------------------------------
# When — construct the client with the configured kwargs from the scenario
# ---------------------------------------------------------------------------


@when("the operator constructs an embed client without explicit pool config")
def _when_no_pool_config(_pool_state: dict[str, Any]) -> None:
    """No kwargs → factory falls back to env/default values."""
    _pool_state["client"] = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
    )


@when(parsers.parse("the operator constructs an embed client with pool size {size:d}"))
def _when_with_pool_size(_pool_state: dict[str, Any], size: int) -> None:
    _pool_state["client"] = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
        pool_max_connections=size,
    )


@when(parsers.parse("the operator constructs an embed client with keepalive {keepalive:d}"))
def _when_with_keepalive(_pool_state: dict[str, Any], keepalive: int) -> None:
    _pool_state["client"] = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
        pool_max_keepalive=keepalive,
    )


@when(parsers.parse("the operator constructs an embed client with pool size {size:d} and keepalive {keepalive:d}"))
def _when_with_both(_pool_state: dict[str, Any], size: int, keepalive: int) -> None:
    _pool_state["client"] = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret — fixed test fixture, not an operator secret
        endpoint=_FOUNDRY_ENDPOINT,
        pool_max_connections=size,
        pool_max_keepalive=keepalive,
    )


# ---------------------------------------------------------------------------
# Then — assert on the post-construction pool state
# ---------------------------------------------------------------------------


def _pool(client: Any) -> Any:
    """Reach the underlying httpx pool — same observable as the integration test.

    openai SDK 2.x exposes the supplied httpx.Client at ``client._client``;
    the transport pool carries ``_max_connections`` /
    ``_max_keepalive_connections``. This pins what the operator actually
    got on the wire, not what we asked for.
    """
    return client._client._transport._pool


@then(parsers.parse("the underlying HTTP pool has at most {n:d} connections"))
def _then_max_connections(_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: drop ``http_client=`` from make_openai_client and the SDK
    falls back to its own client with a different pool-size value, breaking
    this assertion.
    """
    pool = _pool(_pool_state["client"])
    assert pool._max_connections == n, f"expected max_connections={n}, got {pool._max_connections}"


@then(parsers.parse("the underlying HTTP pool keeps at most {n:d} idle connections warm"))
def _then_max_keepalive(_pool_state: dict[str, Any], n: int) -> None:
    """Sabotage: hardcode max_keepalive_connections to a constant and the
    operator-supplied override stops flowing through, breaking this assertion.
    """
    pool = _pool(_pool_state["client"])
    assert pool._max_keepalive_connections == n, f"expected max_keepalive={n}, got {pool._max_keepalive_connections}"
