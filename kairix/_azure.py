"""
Shared Azure OpenAI client for the kairix pipeline.

Provides:
  - embed_text(text: str) -> list[float]
        Embeds text via the configured LLM provider (Azure OpenAI, OpenAI, etc.).
        Returns [] on any failure — callers treat [] as "no embedding available".

Credentials are resolved by ``kairix.credentials.get_credentials()`` which checks:
  1. Direct env vars (KAIRIX_LLM_API_KEY / KAIRIX_EMBED_API_KEY etc.)
  2. Per-file secret (/run/secrets/<name> or ~/.config/kairix/secrets/<name>)
  3. Bundle file (kairix.env — vault-agent sidecar pattern)
  4. Azure Key Vault CLI fallback (KAIRIX_KV_NAME)

Failure modes:
  - Credentials unavailable: returns []
  - Network error: returns []
  - API error (rate limit, auth failure, etc.): returns []
  - Malformed response: returns []
  Never raises.
"""

import logging
from functools import lru_cache
from typing import Any

from kairix.core.db import EMBED_VECTOR_DIMS as EMBED_DIMS
from kairix.secrets import load_secrets as _load_secrets

# Load vault-agent sidecar secrets before any env-var reads.
# No-op when /run/secrets/kairix.env is absent (local dev, CI).
_load_secrets()

logger = logging.getLogger(__name__)

# Default deployment
_DEFAULT_EMBED_DEPLOYMENT = "text-embedding-3-large"

# Embedding API timeout (seconds)
_EMBED_TIMEOUT_S = 30


def _resolve_secret(secret_name: str) -> str | None:
    """Resolve a single secret, returning None on any failure. Never raises or logs values."""
    try:
        from kairix.secrets import get_secret

        return get_secret(secret_name, required=False) or None
    except Exception:
        logger.warning("_azure: failed to resolve a required credential")
        return None


@lru_cache(maxsize=1)
def _get_secrets() -> dict[str, str]:
    """
    Fetch embed credentials via ``get_credentials("embed")``.

    Cached for the process lifetime (lru_cache with maxsize=1).
    Returns {} on any failure — callers check for missing keys.
    Never raises.
    """
    try:
        from kairix.credentials import Credentials, get_credentials

        creds = get_credentials("embed")
        if creds is None or not isinstance(creds, Credentials):
            return {}
        secrets: dict[str, str] = {
            "api_key": creds.api_key,
            "endpoint": creds.endpoint.rstrip("/"),
            "deployment": creds.model or _DEFAULT_EMBED_DEPLOYMENT,
        }
        return secrets
    except Exception:
        logger.warning("_azure: failed to resolve embed credentials")
        return {}


def _get_client() -> Any:
    """Return an OpenAI-compatible client configured from secrets. Cached per-process.

    Detects Azure endpoints automatically. For OpenRouter or standard OpenAI,
    set the endpoint to the base URL (e.g. https://openrouter.ai/api/v1).
    """
    from kairix.credentials import make_openai_client

    secrets = _get_secrets()
    api_key = secrets.get("api_key")
    endpoint = secrets.get("endpoint")
    if not api_key or not endpoint:
        raise ValueError("Missing API key or endpoint")

    return make_openai_client(api_key, endpoint, timeout=float(_EMBED_TIMEOUT_S))


def embed_text(text: str) -> list[float]:
    """
    Embed a text string via Azure OpenAI text-embedding-3-large.

    Returns a list of floats (dimension set by KAIRIX_EMBED_DIMS). Returns [] on any failure.
    Never raises. Uses the OpenAI SDK with built-in retry and backoff.
    """
    if not text or not text.strip():
        return []

    try:
        client = _get_client()
        secrets = _get_secrets()
        deployment = secrets.get("deployment", _DEFAULT_EMBED_DEPLOYMENT)
        response = client.embeddings.create(
            model=deployment,
            input=[text],
            dimensions=EMBED_DIMS,
        )
        return list(response.data[0].embedding)
    except Exception as e:
        logger.warning("embed_text: %s", e)
        return []


def chat_completion(messages: list[dict[str, str]], max_tokens: int = 800) -> str:
    """
    Call GPT-4o-mini for synthesis via Azure OpenAI chat completions.

    Uses the kairix-llm-model secret for the model/deployment name.
    Same endpoint and API key as embeddings.

    Returns empty string on any failure. Never raises.
    Uses the OpenAI SDK with built-in retry and backoff.
    """
    try:
        client = _get_client()
    except Exception as e:
        logger.warning("chat_completion: failed to get client — %s", e)
        return ""

    # Fetch LLM model name via credentials
    try:
        from kairix.credentials import Credentials as _Creds
        from kairix.credentials import get_credentials

        llm_creds = get_credentials("llm")
        deployment = llm_creds.model if isinstance(llm_creds, _Creds) else ""
    except Exception as e:
        logger.warning("chat_completion: error resolving LLM model — %s", e)
        deployment = ""

    if not deployment:
        deployment = "gpt-4o-mini"
        logger.warning("chat_completion: using fallback deployment name %r", deployment)

    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            temperature=0.3,
        )
        content: str = response.choices[0].message.content or ""
        return content
    except Exception as e:
        logger.warning("chat_completion: %s", e)
        return ""


# ---------------------------------------------------------------------------
# ChatBackend protocol adapter (#143 Phase 2a)
#
# Wraps the legacy ``chat_completion`` function in the ``ChatBackend`` protocol
# shape used by the eval module's LLMJudge / QueryGenerator. Production code
# constructs ``AzureChatBackend()`` once and injects it into the eval classes;
# tests inject ``FakeChatBackend`` instead.
#
# Note: ``chat_completion`` resolves credentials internally via the
# ``kairix.credentials`` module — the ``api_key`` / ``endpoint`` args on
# ``complete()`` are accepted for protocol conformance but ignored by this
# adapter. Phase 4 may rework that once all callers route through the protocol.
# ---------------------------------------------------------------------------


class AzureChatBackend:
    """ChatBackend implementation that delegates to ``chat_completion``.

    Translates the protocol's ``(prompt, *, system, ...)`` shape into the
    ``messages=[...]`` shape expected by the Azure / OpenAI chat-completions
    SDK call inside ``chat_completion``. Credentials are resolved by the
    underlying function (vault-agent / env / Key Vault); the ``api_key`` and
    ``endpoint`` kwargs on ``complete()`` are accepted for protocol
    conformance but ignored here — see module-level note.
    """

    def complete(
        self,
        prompt: str,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        system: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 30.0,
    ) -> str:
        # Credentials and tuning kwargs are ChatBackend-protocol fields that
        # this Adapter ignores — chat_completion() resolves credentials from
        # vault-agent / env / Key Vault internally. Mark as intentionally
        # unused so static analyzers don't flag the protocol-conformance
        # surface.
        del api_key, endpoint, deployment, temperature, timeout_s
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return chat_completion(messages)
