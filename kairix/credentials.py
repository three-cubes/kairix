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
from dataclasses import dataclass
from typing import Any

from kairix.paths import azure_api_version as _azure_api_version

logger = logging.getLogger(__name__)


# Env read lives in kairix.paths.azure_api_version (F4 — env reads stay in paths/secrets).
AZURE_API_VERSION = _azure_api_version()


@dataclass(frozen=True)
class Credentials:
    """Resolved provider credentials."""

    api_key: str
    endpoint: str
    model: str
    dims: int = 0  # set from KAIRIX_EMBED_DIMS at resolve time

    @property
    def is_azure(self) -> bool:
        """True if the endpoint is an Azure deployment."""
        ep = self.endpoint.lower()
        return "azure" in ep or "cognitiveservices" in ep


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
) -> Any:
    """Create an OpenAI-compatible client. Auto-detects Azure from the endpoint URL.

    Single factory — all client creation in kairix goes through this function.
    """
    is_azure = "azure" in endpoint.lower() or "cognitiveservices" in endpoint.lower()
    if is_azure:
        from openai import AzureOpenAI

        return AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=AZURE_API_VERSION,
            max_retries=max_retries,
            timeout=timeout,
        )
    else:
        from openai import OpenAI

        return OpenAI(
            api_key=api_key,
            base_url=endpoint,
            max_retries=max_retries,
            timeout=timeout,
        )


def get_credentials(purpose: str) -> Credentials | GraphCredentials | None:
    """Resolve credentials for the given purpose.

    Args:
        purpose: "llm" (chat completions), "embed" (embeddings), or "graph" (Neo4j).

    For "embed": tries embed-specific secrets first, falls back to LLM secrets.
    For "graph": returns None if Neo4j password is not configured.

    Raises:
        OSError: When required credentials (llm, embed) cannot be resolved.
        ValueError: When purpose is not recognised.
    """
    if purpose == "llm":
        return _resolve_llm()
    elif purpose == "embed":
        return _resolve_embed()
    elif purpose == "graph":
        return _resolve_graph()
    else:
        raise ValueError(f"Unknown credential purpose: {purpose!r}. Use 'llm', 'embed', or 'graph'.")


def _resolve_llm() -> Credentials:
    from kairix.secrets import get_secret

    api_key = get_secret("kairix-llm-api-key", required=True)
    endpoint = get_secret("kairix-llm-endpoint", required=True)
    assert api_key is not None  # get_secret raises if required and missing
    assert endpoint is not None
    model = get_secret("kairix-llm-model", required=False) or "gpt-4o-mini"
    return Credentials(api_key=api_key, endpoint=endpoint, model=model)


def _resolve_embed() -> Credentials:
    from kairix.core.db import EMBED_VECTOR_DIMS
    from kairix.secrets import get_secret

    api_key = get_secret("kairix-embed-api-key", required=False)
    endpoint = get_secret("kairix-embed-endpoint", required=False)
    model = get_secret("kairix-embed-model", required=False)

    if not api_key:
        api_key = get_secret("kairix-llm-api-key", required=True)
        assert api_key is not None
    if not endpoint:
        endpoint = get_secret("kairix-llm-endpoint", required=True)
        assert endpoint is not None
    if not model:
        model = "text-embedding-3-large"

    return Credentials(api_key=api_key, endpoint=endpoint, model=model, dims=EMBED_VECTOR_DIMS)


def _resolve_graph() -> GraphCredentials | None:
    from kairix.secrets import get_secret, neo4j_uri, neo4j_user

    uri = neo4j_uri()
    user = neo4j_user()
    password = get_secret("kairix-neo4j-password", required=False)
    if not password:
        return None
    return GraphCredentials(uri=uri, user=user, password=password)
