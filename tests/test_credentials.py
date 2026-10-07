"""Tests for kairix.credentials — resolved Credentials/GraphCredentials.

Env-var resolution is driven through the real :class:`SecretsLoader` built
with an explicit ``env`` mapping + a tmp ``kv_mount`` (F2-clean — no
``monkeypatch.setenv`` of ``KAIRIX_*``), and the HTTP-pool knobs through
``make_openai_client(env=...)``. No @patch on kairix internals.
"""

from __future__ import annotations

import pytest

from kairix.credentials import (
    AZURE_API_VERSION,
    Credentials,
    GraphCredentials,
    get_credentials,
    make_openai_client,
)
from kairix.secrets import SecretNotFoundError, SecretsLoader
from tests.fakes import FakeSecretsLoader


def _env_loader(tmp_path, env: dict[str, str]) -> SecretsLoader:
    """Real production loader scoped to ``env`` with a nonexistent KV mount
    so only the canonical env-var step can resolve (no process-env reads)."""
    return SecretsLoader(env=env, kv_mount=tmp_path / "no-such-dir")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_credentials_is_azure_detection_covers_legacy_and_foundry() -> None:
    """is_azure is True for both legacy Azure-OpenAI and AI Foundry endpoints.

    Sabotage: drop the Foundry fragment from the detection and the foundry
    Credentials assertion fails (Foundry endpoints contain "azure" via
    services.ai.azure.com but the original `is_azure` check predated that
    surface — keeping both alive matters for is_azure-gated code paths).
    """
    azure = Credentials(api_key="k", endpoint="https://x.openai.azure.com", model="m")
    cogn = Credentials(api_key="k", endpoint="https://x.cognitiveservices.azure.com", model="m")
    foundry = Credentials(api_key="k", endpoint="https://x.services.ai.azure.com", model="m")
    openai_direct = Credentials(api_key="k", endpoint="https://api.openai.com/v1", model="m")
    openrouter = Credentials(api_key="k", endpoint="https://openrouter.ai/api/v1", model="m")
    assert azure.is_azure is True
    assert cogn.is_azure is True
    assert foundry.is_azure is True
    assert openai_direct.is_azure is False
    assert openrouter.is_azure is False


@pytest.mark.unit
def test_credentials_is_foundry_distinguishes_foundry_from_legacy_azure() -> None:
    """is_foundry is True only for the unified AI Foundry inference surface.

    Sabotage: collapse is_foundry to alias is_azure and the legacy Azure
    Credentials assertion fails — the two paths are routed differently
    inside make_openai_client and code that needs to know "Foundry yes/no"
    can't lump them together.
    """
    foundry = Credentials(api_key="k", endpoint="https://x.services.ai.azure.com", model="m")
    legacy = Credentials(api_key="k", endpoint="https://x.openai.azure.com", model="m")
    openrouter = Credentials(api_key="k", endpoint="https://openrouter.ai/api/v1", model="m")
    assert foundry.is_foundry is True
    assert legacy.is_foundry is False
    assert openrouter.is_foundry is False


@pytest.mark.unit
def test_graph_credentials_dataclass() -> None:
    gc = GraphCredentials(uri="bolt://x:7687", user="neo4j", password="pw")  # pragma: allowlist secret
    assert gc.uri == "bolt://x:7687"
    assert gc.user == "neo4j"
    assert gc.password == "pw"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# get_credentials dispatch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_credentials_unknown_purpose_raises() -> None:
    """Unknown purpose raises ValueError with hint at valid options."""
    with pytest.raises(ValueError, match="Unknown credential purpose"):
        get_credentials("nonsense")


# ---------------------------------------------------------------------------
# _resolve_llm — happy path + model fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_llm_with_explicit_model(tmp_path) -> None:
    """When all three canonical env vars set, returns a Credentials with that model."""
    env = {
        "KAIRIX_PROVIDER_LLM_API_KEY": "test-key",  # pragma: allowlist secret
        "KAIRIX_PROVIDER_LLM_ENDPOINT": "https://api.openai.com/v1",
        "KAIRIX_PROVIDER_LLM_MODEL": "gpt-4o",
    }

    creds = get_credentials("llm", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, Credentials)
    assert creds.api_key == "test-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://api.openai.com/v1"
    assert creds.model == "gpt-4o"


@pytest.mark.unit
def test_resolve_llm_uses_default_model(tmp_path) -> None:
    """When KAIRIX_PROVIDER_LLM_MODEL is unset, falls back to 'gpt-4o-mini'."""
    env = {
        "KAIRIX_PROVIDER_LLM_API_KEY": "test-key",  # pragma: allowlist secret
        "KAIRIX_PROVIDER_LLM_ENDPOINT": "https://api.openai.com/v1",
    }

    creds = get_credentials("llm", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, Credentials)
    assert creds.model == "gpt-4o-mini"


@pytest.mark.unit
def test_resolve_llm_raises_when_missing(tmp_path) -> None:
    """When required canonical env var missing, raises SecretNotFoundError.

    The credentials module surfaces the loader's typed
    :class:`SecretNotFoundError` (LookupError subclass) — message
    carries the canonical KV name + the loader's F21 fix/next/run markers.
    """
    env: dict[str, str] = {}
    with pytest.raises(SecretNotFoundError):
        get_credentials("llm", secrets=_env_loader(tmp_path, env))


# ---------------------------------------------------------------------------
# _resolve_embed — explicit overrides + fallback to LLM creds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_embed_uses_embed_specific_secrets(tmp_path) -> None:
    env = {
        "KAIRIX_PROVIDER_EMBED_API_KEY": "embed-key",  # pragma: allowlist secret — test fixture value, not a credential
        "KAIRIX_PROVIDER_EMBED_ENDPOINT": "https://embed.example.com",
        "KAIRIX_PROVIDER_EMBED_MODEL": "text-embedding-3-small",
    }

    creds = get_credentials("embed", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, Credentials)
    assert creds.api_key == "embed-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://embed.example.com"
    assert creds.model == "text-embedding-3-small"
    assert creds.dims > 0


@pytest.mark.unit
def test_resolve_embed_falls_back_to_llm_secrets(tmp_path) -> None:
    """When embed-specific creds are missing, falls back to LLM creds."""
    env = {
        "KAIRIX_PROVIDER_LLM_API_KEY": "llm-key",  # pragma: allowlist secret — test fixture value, not a credential
        "KAIRIX_PROVIDER_LLM_ENDPOINT": "https://api.openai.com/v1",
    }

    creds = get_credentials("embed", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, Credentials)
    assert creds.api_key == "llm-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://api.openai.com/v1"
    # Default embed model
    assert creds.model == "text-embedding-3-large"


# ---------------------------------------------------------------------------
# _resolve_graph — None when password absent, GraphCredentials otherwise
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_graph_returns_none_without_password(tmp_path) -> None:
    env: dict[str, str] = {}
    assert get_credentials("graph", secrets=_env_loader(tmp_path, env)) is None


@pytest.mark.unit
def test_resolve_graph_returns_credentials_with_password(tmp_path) -> None:
    env = {
        "KAIRIX_INFRA_NEO4J_PASSWORD": "secret-pw",  # pragma: allowlist secret — test fixture value, not a credential
        "KAIRIX_INFRA_NEO4J_URI": "bolt://neo4j.test:7687",
        "KAIRIX_INFRA_NEO4J_USER": "alice",
    }
    creds = get_credentials("graph", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, GraphCredentials)
    assert creds.uri == "bolt://neo4j.test:7687"
    assert creds.user == "alice"
    assert creds.password == "secret-pw"  # pragma: allowlist secret


@pytest.mark.unit
def test_resolve_graph_uses_default_uri_when_unset(tmp_path) -> None:
    """KAIRIX_INFRA_NEO4J_URI defaults to bolt://localhost:7687 when unset."""
    env = {
        "KAIRIX_INFRA_NEO4J_PASSWORD": "pw",  # pragma: allowlist secret — test fixture value, not a credential
    }
    creds = get_credentials("graph", secrets=_env_loader(tmp_path, env))
    assert isinstance(creds, GraphCredentials)
    assert creds.uri == "bolt://localhost:7687"
    assert creds.user == "neo4j"


# ---------------------------------------------------------------------------
# Canonical surface — secrets= injection seam (FakeSecretsLoader)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_credentials_loads_secrets_via_loader() -> None:
    """get_credentials threads through the injected SecretsResolver.

    Pins the canonical-surface contract: the FakeSecretsLoader's
    get_calls captures every identity tuple the credentials module asks
    for, so the contract surface (which scopes are asked for which
    purpose) is mechanically observable.

    Sabotage-proof: replace ``loader.require`` with hardcoded strings in
    _resolve_llm → the loader's get_calls stays empty and the assertion
    on the api-key identity tuple below flunks.
    """
    loader = FakeSecretsLoader(
        values={
            ("provider", "llm", None, "api-key"): "loader-llm-key",
            ("provider", "llm", None, "endpoint"): "https://loader.example.com",
            ("provider", "llm", None, "model"): "loader-model",
        },
    )
    creds = get_credentials("llm", secrets=loader)
    assert isinstance(creds, Credentials)
    assert creds.api_key == "loader-llm-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://loader.example.com"
    assert creds.model == "loader-model"
    # Loader was asked for each identity tuple
    assert ("provider", "llm", None, "api-key") in loader.get_calls
    assert ("provider", "llm", None, "endpoint") in loader.get_calls
    assert ("provider", "llm", None, "model") in loader.get_calls


@pytest.mark.unit
def test_get_credentials_llm_via_loader_raises_when_required_missing() -> None:
    """When the injected loader can't resolve api-key/endpoint, raises SecretNotFoundError.

    Sabotage-proof: replace ``loader.require(*_SCOPE_LLM_API_KEY)`` with
    ``loader.get(*_SCOPE_LLM_API_KEY) or ""`` → the assertion catches
    that get_credentials no longer raises on missing required secrets.
    """
    loader = FakeSecretsLoader()  # nothing bound
    with pytest.raises(SecretNotFoundError):
        get_credentials("llm", secrets=loader)


@pytest.mark.unit
def test_get_credentials_embed_falls_back_to_llm_via_loader() -> None:
    """Embed-specific creds missing → falls back to llm-scope creds (loader path).

    Sabotage-proof: drop the ``if not api_key: api_key = loader.require(...)``
    fallback in _resolve_embed → the embed-creds api_key stays None /
    empty and the resulting Credentials has the wrong value.
    """
    loader = FakeSecretsLoader(
        values={
            ("provider", "llm", None, "api-key"): "llm-fallback-key",
            ("provider", "llm", None, "endpoint"): "https://llm.example.com",
        },
    )
    creds = get_credentials("embed", secrets=loader)
    assert isinstance(creds, Credentials)
    assert creds.api_key == "llm-fallback-key"  # pragma: allowlist secret
    assert creds.endpoint == "https://llm.example.com"
    # Default embed model when neither embed-model nor an override resolves.
    assert creds.model == "text-embedding-3-large"


@pytest.mark.unit
def test_get_credentials_graph_via_loader_returns_none_without_password() -> None:
    """Loader with no neo4j password → graph credentials resolve to None.

    Sabotage-proof: change ``if not password: return None`` to ``return
    GraphCredentials(...)`` and the assertion catches that we now build
    invalid credentials with a missing password.
    """
    loader = FakeSecretsLoader()  # nothing bound
    creds = get_credentials("graph", secrets=loader)
    assert creds is None


@pytest.mark.unit
def test_get_credentials_graph_via_loader_resolves_full_triple() -> None:
    """Loader with neo4j password+uri+user → GraphCredentials with those values.

    Sabotage-proof: hardcode ``user = "neo4j"`` in _resolve_graph and the
    assertion on creds.user catches the override that should have come
    from the loader.
    """
    loader = FakeSecretsLoader(
        values={
            ("infra", "neo4j", None, "password"): "loader-pw",
            ("infra", "neo4j", None, "uri"): "bolt://loader.example:7687",
            ("infra", "neo4j", None, "user"): "loader-user",
        },
    )
    creds = get_credentials("graph", secrets=loader)
    assert isinstance(creds, GraphCredentials)
    assert creds.password == "loader-pw"  # pragma: allowlist secret
    assert creds.uri == "bolt://loader.example:7687"
    assert creds.user == "loader-user"


@pytest.mark.unit
def test_get_credentials_graph_loader_defaults_uri_and_user() -> None:
    """Loader with only the neo4j password → URI/user fall back to defaults.

    Sabotage-proof: remove the ``or "bolt://localhost:7687"`` fallback in
    _resolve_graph and the URI flips to falsy/empty rather than the
    documented default.
    """
    loader = FakeSecretsLoader(
        values={
            ("infra", "neo4j", None, "password"): "only-pw",
        },
    )
    creds = get_credentials("graph", secrets=loader)
    assert isinstance(creds, GraphCredentials)
    assert creds.password == "only-pw"  # pragma: allowlist secret
    assert creds.uri == "bolt://localhost:7687"
    assert creds.user == "neo4j"


# ---------------------------------------------------------------------------
# make_openai_client
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_openai_client_returns_azure_client_for_azure_endpoint() -> None:
    """Endpoint containing 'azure' triggers the AzureOpenAI factory."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.openai.azure.com",
    )
    # AzureOpenAI sets api_version internally
    assert client is not None
    cls_name = type(client).__name__
    assert "Azure" in cls_name


@pytest.mark.unit
def test_make_openai_client_returns_openai_client_for_openai_endpoint() -> None:
    """Endpoint without 'azure' triggers the OpenAI factory."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
    )
    assert client is not None
    cls_name = type(client).__name__
    assert cls_name == "OpenAI"


@pytest.mark.unit
def test_make_openai_client_detects_cognitiveservices_as_azure() -> None:
    """cognitiveservices endpoint is treated as Azure."""
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.cognitiveservices.azure.com",
    )
    assert "Azure" in type(client).__name__


@pytest.mark.unit
def test_make_openai_client_foundry_routes_through_openai_compat_alias() -> None:
    """``services.ai.azure.com`` endpoints use the OpenAI SDK with the /openai/v1 alias.

    Sabotage: re-order the detection so legacy-Azure-fragments are checked
    before Foundry, and a Foundry endpoint (which contains "azure" too) gets
    misrouted into AzureOpenAI — the class-name assertion below catches that
    (AzureOpenAI returns "AzureOpenAI", not "OpenAI").
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com",
    )
    assert type(client).__name__ == "OpenAI", f"expected plain OpenAI client; got {type(client).__name__}"
    # base_url must carry the openai-compat alias suffix so requests hit the
    # right path on the Foundry surface.
    assert str(client.base_url).rstrip("/").endswith("/openai/v1"), (
        f"expected base_url to end with /openai/v1; got {client.base_url}"
    )


@pytest.mark.unit
def test_make_openai_client_foundry_respects_explicit_openai_v1_suffix() -> None:
    """If the operator's configured endpoint already includes /openai/v1, don't double-append.

    Sabotage: change the ``if not base_url.endswith(...)`` guard to an
    unconditional append and the assertion catches the resulting
    ``/openai/v1/openai/v1`` path that would 404 on Azure.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com/openai/v1",
    )
    assert "/openai/v1/openai/v1" not in str(client.base_url)
    assert str(client.base_url).rstrip("/").endswith("/openai/v1")


@pytest.mark.unit
def test_make_openai_client_legacy_azure_endpoint_still_routes_to_azure_openai() -> None:
    """Legacy ``<r>.openai.azure.com`` endpoints keep using AzureOpenAI with azure_endpoint.

    Sabotage: drop the legacy-Azure branch and the existing deployments
    using the legacy URL (operators with longer-lived secrets, the
    ``dan-mo5ez6sn-eastus2`` resource) start hitting the OpenAI-direct
    branch and 401 on the API key shape.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.openai.azure.com",
    )
    assert "Azure" in type(client).__name__


@pytest.mark.unit
def test_azure_api_version_constant() -> None:
    """AZURE_API_VERSION is non-empty (read at module import via env or default)."""
    assert AZURE_API_VERSION
    assert isinstance(AZURE_API_VERSION, str)


# ---------------------------------------------------------------------------
# HTTP-pool tuning (#280 — Tier 1 lever 1)
#
# These tests pin the operator-tunable pool knobs that resolve Azure embed
# pool contention at peak teaming concurrency. Probe data showed vector
# latency growing 240 → 534 ms going conc=1 → conc=10 with the openai SDK's
# default httpx Limits — these knobs let operators tune for their load.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_openai_client_passes_http_client_with_limits() -> None:
    """Foundry endpoint client carries a custom httpx.Client (not SDK default).

    Sabotage: drop the ``http_client=`` kwarg from the Foundry branch in
    ``make_openai_client`` and the openai SDK falls back to its internal
    ``SyncHttpxClientWrapper``; ``type(client._client).__name__`` becomes
    "SyncHttpxClientWrapper" instead of "Client" and the assertion fails.
    """
    import httpx

    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://example.services.ai.azure.com",
    )
    # Plain httpx.Client (our build) vs SyncHttpxClientWrapper (SDK default)
    assert type(client._client).__name__ == "Client", (
        f"expected plain httpx.Client; got {type(client._client).__name__}"
    )
    assert isinstance(client._client, httpx.Client)


@pytest.mark.unit
def test_http_client_limits_match_configured_pool_size() -> None:
    """The httpx pool on the resulting client matches the configured triple.

    Drives through the ``make_openai_client`` public surface — no internal
    helper imports — by setting all three pool env vars and asserting the
    resulting transport pool carries those exact values.

    Sabotage: hardcode ``max_connections=5`` in ``_build_http_client`` and
    the pool ``_max_connections`` no longer matches the requested 20 — the
    assertion fails.
    """
    env = {
        "KAIRIX_EMBED_POOL_SIZE": "20",
        "KAIRIX_EMBED_POOL_KEEPALIVE": "10",
        "KAIRIX_EMBED_POOL_EXPIRY_S": "30.0",
    }
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        env=env,
    )
    pool = client._client._transport._pool
    assert pool._max_connections == 20
    assert pool._max_keepalive_connections == 10
    assert pool._keepalive_expiry == 30.0


@pytest.mark.unit
def test_default_pool_size_is_20_when_secret_unset() -> None:
    """``KAIRIX_EMBED_POOL_SIZE`` unset → default of 20 is used.

    Drives through the public ``make_openai_client`` surface so the
    default-resolution path inside ``_resolve_pool_config`` is exercised
    end-to-end (no internal-name imports).

    Sabotage: change the default constant in ``kairix/credentials.py``
    from ``EMBED_POOL_MAX_CONNECTIONS = 20`` to another value and the
    resulting pool no longer reports 20 — the assertion fails.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        env={},
    )
    pool = client._client._transport._pool
    assert pool._max_connections == 20


@pytest.mark.unit
def test_pool_size_overrides_default_when_secret_set() -> None:
    """``KAIRIX_EMBED_POOL_SIZE=50`` → resolved client pool uses 50 connections.

    Sabotage: stop wiring ``_resolve_pool_config()`` into
    ``make_openai_client`` (revert the http_client to the openai SDK
    default) and the pool ``_max_connections`` reverts to the SDK default
    of 1000 — the assertion fails.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        env={"KAIRIX_EMBED_POOL_SIZE": "50"},
    )
    pool = client._client._transport._pool
    assert pool._max_connections == 50


@pytest.mark.unit
def test_pool_size_invalid_falls_back_to_default(caplog) -> None:
    """Non-integer ``KAIRIX_EMBED_POOL_SIZE`` → falls back to default 20 with a logged warning.

    Sabotage: remove the ``try/except ValueError`` in
    ``kairix.paths.embed_pool_size`` and the int() call raises on "abc",
    crashing the test instead of asserting the fallback.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        client = make_openai_client(
            api_key="test-key",  # pragma: allowlist secret
            endpoint="https://api.openai.com/v1",
            env={"KAIRIX_EMBED_POOL_SIZE": "abc"},
        )
    pool = client._client._transport._pool
    assert pool._max_connections == 20
    assert any("KAIRIX_EMBED_POOL_SIZE" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_explicit_pool_kwarg_overrides_env_value() -> None:
    """``pool_max_connections=99`` kwarg wins even when env sets a different value.

    Pins the precedence contract: explicit kwargs beat env fallback.
    The env value arrives through the ``env=`` mapping seam (F2-clean).
    The kwarg-precedence rule
    lives in production code (``_resolve_pool_config`` uses ``... if
    pool_max_connections is None else pool_max_connections``).

    Sabotage: invert that conditional so env beats kwarg, and the kwarg's
    99 value loses to the env's 7.
    """
    client = make_openai_client(
        api_key="test-key",  # pragma: allowlist secret
        endpoint="https://api.openai.com/v1",
        pool_max_connections=99,
        env={"KAIRIX_EMBED_POOL_SIZE": "7"},
    )
    pool = client._client._transport._pool
    assert pool._max_connections == 99
