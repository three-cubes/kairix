"""
Central credential resolution for kairix.

One function, one import. Every module that needs credentials calls::

    from kairix.credentials import get_credentials

    creds = get_credentials("embed")  # or "llm" or "graph"
    client = OpenAI(api_key=creds.api_key, base_url=creds.endpoint)

Embed credentials fall back to LLM credentials when not set separately.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from kairix.paths import (
    azure_api_version as _azure_api_version,
)
from kairix.paths import (
    embed_pool_expiry_s as _embed_pool_expiry_s,
)
from kairix.paths import (
    embed_pool_keepalive as _embed_pool_keepalive,
)
from kairix.paths import (
    embed_pool_size as _embed_pool_size,
)
from kairix.secrets import Scope, SecretsResolver

logger = logging.getLogger(__name__)

# Canonical secret identity tuples. The loader resolves each via the
# canonical ``KAIRIX_<SCOPE>_<AREA>_<LEAF>`` env var or a KV-mount
# file at ``/run/kairix/secrets/<canonical-name>``. The historical
# alias chain was retired in #369; operators with pre-canonical env
# vars must rotate to the canonical names.
_SCOPE_LLM_API_KEY: tuple[Scope, str, str | None, str] = ("provider", "llm", None, "api-key")
_SCOPE_LLM_ENDPOINT: tuple[Scope, str, str | None, str] = ("provider", "llm", None, "endpoint")
_SCOPE_LLM_MODEL: tuple[Scope, str, str | None, str] = ("provider", "llm", None, "model")
_SCOPE_EMBED_API_KEY: tuple[Scope, str, str | None, str] = ("provider", "embed", None, "api-key")
_SCOPE_EMBED_ENDPOINT: tuple[Scope, str, str | None, str] = ("provider", "embed", None, "endpoint")
_SCOPE_EMBED_MODEL: tuple[Scope, str, str | None, str] = ("provider", "embed", None, "model")
_SCOPE_NEO4J_PASSWORD: tuple[Scope, str, str | None, str] = ("infra", "neo4j", None, "password")
_SCOPE_NEO4J_URI: tuple[Scope, str, str | None, str] = ("infra", "neo4j", None, "uri")
_SCOPE_NEO4J_USER: tuple[Scope, str, str | None, str] = ("infra", "neo4j", None, "user")


# Env read lives in kairix.paths.azure_api_version (F4 — env reads stay in paths/secrets).
AZURE_API_VERSION = _azure_api_version()


# Pool config - kairix-side knobs that operators can tune via Azure Key
# Vault secrets. Defaults sized for peak teaming concurrency (20 agents,
# 5-15 sustained) with headroom. The keep-alive count balances connection
# reuse against socket churn under burst load. Stage-timing data (probe
# sweeps in tier-1 lever 1) showed vector latency grows 240 -> 534 ms going
# from conc=1 -> conc=10 with the openai SDK default httpx Limits - classic
# pool contention. These knobs let operators tune for their concurrency
# profile without code changes.
EMBED_POOL_MAX_CONNECTIONS = 20
EMBED_POOL_MAX_KEEPALIVE = 10
EMBED_POOL_KEEPALIVE_EXPIRY_S = 30.0


def _build_http_client(
    max_connections: int,
    max_keepalive: int,
    expiry_s: float,
    timeout: float,
) -> Any:
    """Return an ``httpx.Client`` with explicit ``Limits``, sized for kairix's teaming load.

    Local import keeps httpx out of the kairix critical-path imports for
    code that uses credentials/auth but doesn't issue HTTP calls.
    """
    import httpx

    limits = httpx.Limits(
        max_connections=max_connections,
        max_keepalive_connections=max_keepalive,
        keepalive_expiry=expiry_s,
    )
    return httpx.Client(limits=limits, timeout=timeout)


def _resolve_pool_config(
    pool_max_connections: int | None = None,
    pool_max_keepalive: int | None = None,
    pool_expiry_s: float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[int, int, float]:
    """Resolve the pool-size triple, preferring explicit kwargs over env reads.

    Production callers leave all three None and the function reads from
    kairix.paths (F4 boundary — env reads stay there). Tests pass explicit
    values to drive ``make_openai_client`` without going through
    ``monkeypatch.setenv`` (F2 — env state shouldn't leak between tests).

    Invalid env values fall back to the module-level defaults with a
    logged warning rather than crashing the embed dispatch stage; that
    fallback behaviour is unit-tested in tests/test_paths.py. ``env`` is
    threaded to the kairix.paths readers (``None`` = live ``os.environ``).
    """
    return (
        _embed_pool_size(EMBED_POOL_MAX_CONNECTIONS, env=env) if pool_max_connections is None else pool_max_connections,
        _embed_pool_keepalive(EMBED_POOL_MAX_KEEPALIVE, env=env) if pool_max_keepalive is None else pool_max_keepalive,
        _embed_pool_expiry_s(EMBED_POOL_KEEPALIVE_EXPIRY_S, env=env) if pool_expiry_s is None else pool_expiry_s,
    )


# Three endpoint shapes kairix routes through ``make_openai_client``:
#
# 1. **Azure AI Foundry** — ``<resource>.services.ai.azure.com`` — Microsoft's
#    unified inference surface, the forward-recommended path. We use the
#    OpenAI-compatible alias at ``/openai/v1`` so the existing openai SDK
#    keeps working. Native AI Inference SDK migration tracked separately for
#    multi-provider (Bedrock / Cohere / Mistral) needs.
# 2. **Legacy Azure OpenAI** — ``<resource>.openai.azure.com`` — the older
#    Azure-OpenAI-specific endpoint. Still supported by Microsoft today;
#    being steered off toward Foundry. Uses ``AzureOpenAI(azure_endpoint=...)``.
# 3. **OpenAI-direct / OpenRouter / other OpenAI-compat** — any other
#    endpoint. Uses ``OpenAI(base_url=...)``.
#
# Ordered detection matters: Foundry endpoints contain "azure" too, so the
# Foundry check fires FIRST. Without that ordering, Foundry endpoints would
# be misrouted into ``AzureOpenAI(azure_endpoint=...)``, which expects the
# legacy URL pattern and would 404 on the embed call.
_FOUNDRY_HOST_FRAGMENT = "services.ai.azure.com"
_FOUNDRY_OPENAI_COMPAT_SUFFIX = "/openai/v1"
_LEGACY_AZURE_FRAGMENTS = ("openai.azure.com", "cognitiveservices.azure.com")


def _is_foundry_endpoint(endpoint: str) -> bool:
    """True for Azure AI Foundry endpoints (``services.ai.azure.com``)."""
    return _FOUNDRY_HOST_FRAGMENT in endpoint.lower()


def _is_legacy_azure_endpoint(endpoint: str) -> bool:
    """True for legacy Azure OpenAI endpoints (``<r>.openai.azure.com`` etc)."""
    ep = endpoint.lower()
    return any(frag in ep for frag in _LEGACY_AZURE_FRAGMENTS) and not _is_foundry_endpoint(endpoint)


@dataclass(frozen=True)
class Credentials:
    """Resolved provider credentials."""

    api_key: str
    endpoint: str
    model: str
    dims: int = 0  # set from KAIRIX_EMBED_DIMS at resolve time

    @property
    def is_azure(self) -> bool:
        """True for any Azure-hosted endpoint (Foundry or legacy)."""
        return _is_foundry_endpoint(self.endpoint) or _is_legacy_azure_endpoint(self.endpoint)

    @property
    def is_foundry(self) -> bool:
        """True specifically for the Azure AI Foundry unified-inference surface."""
        return _is_foundry_endpoint(self.endpoint)


@dataclass(frozen=True)
class GraphCredentials:
    """Resolved Neo4j credentials."""

    uri: str
    user: str
    password: str


def make_openai_client(
    api_key: str,
    endpoint: str,
    *,
    max_retries: int = 5,
    timeout: float = 30.0,
    pool_max_connections: int | None = None,
    pool_max_keepalive: int | None = None,
    pool_expiry_s: float | None = None,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Create an OpenAI-compatible client for any of the three endpoint shapes.

    See the module-level comment block above this function for the three
    branches (Foundry / legacy Azure / OpenAI-direct). The Foundry branch
    uses the ``/openai/v1`` alias so the openai SDK can call AI Foundry
    without a Microsoft-specific SDK dependency.

    Pool config (``pool_max_connections`` / ``pool_max_keepalive`` /
    ``pool_expiry_s``) — when None (production default), reads from
    ``kairix.paths`` env helpers. Tests pass explicit values directly to
    avoid ``monkeypatch.setenv`` (F2).

    ``env`` is the env mapping the pool readers consult when a pool kwarg
    is ``None`` — ``None`` (production) is the live ``os.environ``; tests
    pass a dict to drive the ``KAIRIX_EMBED_POOL_*`` resolution (F2-clean).
    """
    max_conns, max_keepalive, expiry_s = _resolve_pool_config(
        pool_max_connections, pool_max_keepalive, pool_expiry_s, env=env
    )
    http_client = _build_http_client(max_conns, max_keepalive, expiry_s, timeout)

    if _is_foundry_endpoint(endpoint):
        from openai import OpenAI

        base_url = endpoint.rstrip("/")
        # Add the openai-compat alias suffix if the operator didn't already
        # include it — tolerates both forms in the configured secret.
        if not base_url.endswith(_FOUNDRY_OPENAI_COMPAT_SUFFIX):
            base_url = base_url + _FOUNDRY_OPENAI_COMPAT_SUFFIX
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    if _is_legacy_azure_endpoint(endpoint):
        from openai import AzureOpenAI

        return AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=AZURE_API_VERSION,
            max_retries=max_retries,
            timeout=timeout,
            http_client=http_client,
        )

    from openai import OpenAI

    return OpenAI(
        api_key=api_key,
        base_url=endpoint,
        max_retries=max_retries,
        timeout=timeout,
        http_client=http_client,
    )


def get_credentials(
    purpose: str,
    *,
    secrets: SecretsResolver | None = None,
) -> Credentials | GraphCredentials | None:
    """Resolve credentials for the given purpose.

    Args:
        purpose: "llm" (chat completions), "embed" (embeddings), or "graph" (Neo4j).
        secrets: optional :class:`SecretsResolver` injection seam. When
            ``None`` (production default), a :class:`SecretsLoader` is
            constructed lazily — its canonical env-var / KV mount chain
            resolves the credentials. Tests inject
            :class:`tests.fakes.FakeSecretsLoader` so no env-var monkey-
            patching is needed (F2-clean).

    For "embed": tries embed-specific secrets first, falls back to LLM secrets.
    For "graph": returns None if Neo4j password is not configured.

    Raises:
        kairix.secrets.SecretNotFoundError: When required credentials cannot
            be resolved via the injected loader (canonical surface).
        ValueError: When purpose is not recognised.
    """
    loader = secrets if secrets is not None else _default_secrets_loader()
    if purpose == "llm":
        return _resolve_llm(loader)
    elif purpose == "embed":
        return _resolve_embed(loader)
    elif purpose == "graph":
        return _resolve_graph(loader)
    else:
        raise ValueError(f"Unknown credential purpose: {purpose!r}. Use 'llm', 'embed', or 'graph'.")


def _default_secrets_loader() -> SecretsResolver:
    """Build the production :class:`SecretsLoader` lazily.

    Local import keeps the credentials module importable without paying
    for the loader's env-snapshot work on every kairix import. The
    loader's resolution chain (canonical env var -> KV mount) is the
    canonical secrets package's only surface; operators must supply
    credentials under the canonical names.
    """
    from kairix.secrets import SecretsLoader

    return SecretsLoader()


def _resolve_credentials_for_scope(
    loader: SecretsResolver,
    *,
    api_key_scope: tuple[Scope, str, str | None, str],
    endpoint_scope: tuple[Scope, str, str | None, str],
    model_scope: tuple[Scope, str, str | None, str],
    fallback_api_key_scope: tuple[Scope, str, str | None, str] | None = None,
    fallback_endpoint_scope: tuple[Scope, str, str | None, str] | None = None,
    default_model: str,
) -> tuple[str, str, str]:
    """Resolve ``(api_key, endpoint, model)`` for one provider scope.

    Shared skeleton extracted from ``_resolve_llm`` and ``_resolve_embed``
    (which differed only in the per-scope tuples + the embed-tier's
    fallback to LLM credentials). Single source of truth for the
    canonical resolve sequence: prefer primary scope; fall back to
    secondary if primary missing; require non-empty result for api_key
    + endpoint; default-string for model.

    The ``require`` semantics on the fallback (rather than the primary)
    preserves the embed-tier behaviour: an operator who only configured
    LLM creds (and skipped embed creds entirely) gets the LLM creds for
    both tiers, with a useful ``SecretNotFoundError`` naming the LLM
    scope (not the embed scope) when nothing is configured.
    """
    api_key = loader.get(*api_key_scope)
    endpoint = loader.get(*endpoint_scope)
    model = loader.get(*model_scope)

    if not api_key:
        if fallback_api_key_scope is not None:
            api_key = loader.require(*fallback_api_key_scope)
        else:
            api_key = loader.require(*api_key_scope)
    if not endpoint:
        if fallback_endpoint_scope is not None:
            endpoint = loader.require(*fallback_endpoint_scope)
        else:
            endpoint = loader.require(*endpoint_scope)
    if not model:
        model = default_model
    return api_key, endpoint, model


def _resolve_llm(loader: SecretsResolver) -> Credentials:
    api_key, endpoint, model = _resolve_credentials_for_scope(
        loader,
        api_key_scope=_SCOPE_LLM_API_KEY,
        endpoint_scope=_SCOPE_LLM_ENDPOINT,
        model_scope=_SCOPE_LLM_MODEL,
        default_model="gpt-4o-mini",
    )
    return Credentials(api_key=api_key, endpoint=endpoint, model=model)


def _resolve_embed(loader: SecretsResolver) -> Credentials:
    from kairix.core.db import EMBED_VECTOR_DIMS

    api_key, endpoint, model = _resolve_credentials_for_scope(
        loader,
        api_key_scope=_SCOPE_EMBED_API_KEY,
        endpoint_scope=_SCOPE_EMBED_ENDPOINT,
        model_scope=_SCOPE_EMBED_MODEL,
        fallback_api_key_scope=_SCOPE_LLM_API_KEY,
        fallback_endpoint_scope=_SCOPE_LLM_ENDPOINT,
        default_model="text-embedding-3-large",
    )
    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=EMBED_VECTOR_DIMS)


def _resolve_graph(loader: SecretsResolver) -> GraphCredentials | None:
    password = loader.get(*_SCOPE_NEO4J_PASSWORD)
    if not password:
        return None
    uri = loader.get(*_SCOPE_NEO4J_URI) or "bolt://localhost:7687"
    user = loader.get(*_SCOPE_NEO4J_USER) or "neo4j"
    return GraphCredentials(uri=uri, user=user, password=password)
