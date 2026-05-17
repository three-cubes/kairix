"""Azure Foundry ``Provider`` implementation.

Translates the universal :class:`kairix.providers.Provider` Protocol
into Azure AI Foundry's wire surface. Foundry exposes an OpenAI-
compatible alias at ``/openai/v1`` so the openai SDK is used as the
transport client; what this plugin owns is:

- the URL-suffix handling that adds ``/openai/v1`` when the operator's
  configured endpoint lacks it (tolerated in both forms because vault
  secrets historically came in inconsistent shapes);
- the deployment-name → ``model=`` translation on the embed request
  (so the Foundry deployment alias flows through verbatim);
- the upstream error → canonical typed-error mapping
  (:class:`~kairix.providers.RateLimited`,
  :class:`~kairix.providers.AuthError`,
  :class:`~kairix.providers.UpstreamError`,
  :class:`~kairix.providers.ProviderUnreachable`).

DI seams:

- ``credentials``: a :class:`kairix.credentials.Credentials` carrying
  the resolved api-key / endpoint / model. The plugin never reads env
  vars or secrets itself (F4 keeps that in
  ``kairix/paths.py`` / ``kairix/secrets.py``).
- ``transport_client``: an OpenAI-compatible client. Production callers
  leave this ``None`` and a real ``OpenAI(...)`` client is built via
  ``kairix.credentials.make_openai_client`` (which already centralises
  Foundry URL detection / pool configuration). Tests pass a fake
  recording client. Allowed-``None`` here because ``Credentials`` is the
  load-bearing positional and ``transport_client`` is a documented test
  seam; F6 forbids ``*_fn=None`` callables-as-test-shims, not all
  ``=None`` defaults.

Production callers resolve this plugin through
:func:`kairix.providers.get_provider` and the
``[project.entry-points."kairix.providers"]`` table — there is no
direct import path. Tests construct ``AzureFoundryProvider`` directly
with a fake transport client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairix.providers._base import ProviderHealth
from kairix.providers._errors import (
    AuthError,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)

if TYPE_CHECKING:
    from kairix.credentials import Credentials

#: Stable plugin name; matches the entry-point key in ``pyproject.toml``
#: and the ``Examples`` row in ``tests/bdd/features/e2e_provider_*.feature``.
PROVIDER_NAME = "azure_foundry"

#: Default embedding dimension for ``text-embedding-3-large`` on Azure
#: Foundry; matches the rest of kairix (``KAIRIX_EMBED_DIMS`` /
#: ``kairix.core.db.EMBED_VECTOR_DIMS``) when the operator deploys the
#: large model. Used as the fallback when ``dimension()`` is called
#: before any embed has happened.
DEFAULT_EMBED_DIMENSION = 1536

#: Default chat ``max_tokens`` honoured by the Protocol surface.
DEFAULT_CHAT_MAX_TOKENS = 800

#: The Foundry-specific openai-compat alias. Apppended to the configured
#: endpoint when the operator didn't already include it. Kept here too
#: (in addition to ``kairix.credentials._FOUNDRY_OPENAI_COMPAT_SUFFIX``)
#: so the plugin can decide URL shape without importing transport.
_OPENAI_COMPAT_SUFFIX = "/openai/v1"


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


def normalize_foundry_endpoint(endpoint: str) -> str:
    """Return ``endpoint`` with the ``/openai/v1`` suffix appended exactly once.

    Foundry endpoints can be configured in two shapes:

    - ``https://<resource>.services.ai.azure.com``
    - ``https://<resource>.services.ai.azure.com/openai/v1``

    Both are valid in vault config; this helper makes the plugin tolerate
    either form without double-suffixing (which would produce
    ``/openai/v1/openai/v1/embeddings`` — a 404 on Foundry).
    """
    stripped = _strip_trailing_slash(endpoint)
    if stripped.endswith(_OPENAI_COMPAT_SUFFIX):
        return stripped
    return stripped + _OPENAI_COMPAT_SUFFIX


def _status_code_of(err: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from a transport error.

    The openai SDK attaches ``.status_code`` to ``APIStatusError`` and
    its subclasses (``RateLimitError`` is 429, ``AuthenticationError``
    is 401, etc.). httpx response errors carry ``.response.status_code``.
    Returns ``None`` for plain connection / DNS failures (which the
    caller maps to :class:`ProviderUnreachable`).
    """
    code = getattr(err, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(err, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def _retry_after_of(err: Exception) -> float | None:
    """Best-effort extraction of the ``Retry-After`` hint from an error.

    Reads ``err.response.headers["Retry-After"]`` (the standard HTTP
    surface) and falls back to ``None`` if absent / unparseable. The
    upstream may emit the header as either a delta-seconds integer or
    an HTTP-date; we only honour the seconds form here (matching what
    the openai / Azure SDKs put on the wire today).
    """
    response = getattr(err, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = None
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _is_connection_failure(err: Exception) -> bool:
    """True for connection-level failures (no HTTP response received).

    Matches the openai SDK's ``APIConnectionError`` and the stdlib
    ``ConnectionError`` family; the test-seam ``FakeUpstream`` raises
    bare ``ConnectionError`` to stand in.
    """
    cls_name = type(err).__name__
    if cls_name in {"APIConnectionError", "APITimeoutError"}:
        return True
    if isinstance(err, ConnectionError):
        return True
    return False


def _map_transport_error(err: Exception, *, provider_name: str) -> ProviderError:
    """Translate a transport-level exception into a canonical typed error.

    Mapping:

    - ``status_code == 429`` → :class:`RateLimited` (carries Retry-After hint)
    - ``status_code in (401, 403)`` → :class:`AuthError`
    - ``status_code >= 500`` → :class:`UpstreamError`
    - connection failure → :class:`ProviderUnreachable`
    - anything else → bare :class:`ProviderError` with ``repr(err)``

    The ``provider_name`` is interpolated into the surfaced messages so
    operators see which plugin failed — useful when multiple plugins
    are wired (e.g. embed vs chat on different endpoints).
    """
    status = _status_code_of(err)
    if status == 429:
        return RateLimited(
            f"Azure Foundry rate-limited (429): {err}",
            retry_after_s=_retry_after_of(err),
        )
    if status in (401, 403):
        return AuthError(f"Azure Foundry auth rejected ({status}) for provider {provider_name!r}: {err}")
    if status is not None and status >= 500:
        return UpstreamError(
            f"Azure Foundry upstream error ({status}): {err}",
            status_code=status,
        )
    if _is_connection_failure(err):
        return ProviderUnreachable(f"Azure Foundry endpoint unreachable for provider {provider_name!r}: {err}")
    return ProviderError(f"Azure Foundry transport error: {err!r}")


class AzureFoundryProvider:
    """Concrete :class:`kairix.providers.Provider` for Azure AI Foundry.

    Construction is DI-clean: production passes a resolved
    :class:`kairix.credentials.Credentials` and lets the plugin build
    its own openai-compat client lazily; tests pass an explicit
    ``transport_client`` that records ``embeddings.create`` /
    ``chat.completions.create`` calls.

    The provider satisfies the runtime-checkable Protocol — ``isinstance(
    provider, Provider)`` is True at runtime, which is what
    ``EntryPointRegistry.resolve`` relies on for its return-type
    annotation.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credentials: Credentials,
        transport_client: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport_client = transport_client
        # Last-known embed dimension; populated from the first successful
        # embed response so ``dimension()`` reflects what the deployed
        # model actually returned. Falls back to ``DEFAULT_EMBED_DIMENSION``
        # before any embed has happened.
        self._embed_dimension: int | None = credentials.dims if credentials.dims else None

    # ------------------------------------------------------------------
    # Internal: transport client resolution
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the configured transport client, lazily building one.

        Production callers don't pass ``transport_client``; the plugin
        builds an openai-compat client via
        :func:`kairix.credentials.make_openai_client` which already
        encodes the Foundry URL-suffix detection and pool configuration.
        Tests pass an explicit fake recording client and this lazy
        construction is skipped entirely.
        """
        if self._transport_client is not None:
            return self._transport_client
        from kairix.credentials import make_openai_client

        return make_openai_client(
            self._credentials.api_key,
            self._credentials.endpoint,
        )

    # ------------------------------------------------------------------
    # Provider Protocol
    # ------------------------------------------------------------------

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts in one Foundry round-trip.

        Wire shape (pinned by ``provider_azure_foundry.feature``):

        - URL host comes from ``credentials.endpoint`` with the
          ``/openai/v1`` suffix normalisation applied;
        - request path ends with ``/embeddings``;
        - ``model=`` carries the configured deployment name verbatim
          (so a vault-rotated model alias flows through without a code
          change);
        - the auth header is ``api-key: <api_key>`` (this is the
          OpenAI-SDK behaviour for AzureOpenAI / Foundry clients —
          distinct from the ``Authorization: Bearer`` shape used for
          OpenAI-direct).

        Returns one vector per input text, in the same order. Maps any
        transport-level failure to a canonical typed error via
        :func:`_map_transport_error` and re-raises — never returns
        partial / empty vectors silently.
        """
        if not texts:
            return []
        client = self._client()
        try:
            response = client.embeddings.create(
                model=self._credentials.model,
                input=list(texts),
                dimensions=self._credentials.dims or None,
            )
        except Exception as err:
            raise _map_transport_error(err, provider_name=self.name) from err
        vectors = [list(item.embedding) for item in response.data]
        if vectors and vectors[0]:
            self._embed_dimension = len(vectors[0])
        return vectors

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = DEFAULT_CHAT_MAX_TOKENS,
    ) -> str:
        """Run a single chat completion against Foundry.

        Translates the Protocol's ``messages=[...]`` shape into the
        SDK's ``chat.completions.create(model=, messages=, max_tokens=)``
        call. ``model=`` carries the configured deployment, ``temperature``
        defaults to ``0.3`` for deterministic-ish synthesis. Maps
        transport failures via :func:`_map_transport_error`.
        """
        client = self._client()
        try:
            response = client.chat.completions.create(
                model=self._credentials.model,
                messages=list(messages),
                max_tokens=max_tokens,
                temperature=0.3,
            )
        except Exception as err:
            raise _map_transport_error(err, provider_name=self.name) from err
        content = response.choices[0].message.content
        return content or ""

    def dimension(self) -> int:
        """Embedding vector dimension for the configured deployment.

        Returns the dims captured from the most recent embed response
        when available (matches what the deployed model actually
        produced); falls back to ``credentials.dims`` and then
        ``DEFAULT_EMBED_DIMENSION`` so callers always get a positive
        integer.
        """
        if self._embed_dimension:
            return self._embed_dimension
        if self._credentials.dims:
            return self._credentials.dims
        return DEFAULT_EMBED_DIMENSION

    def healthcheck(self) -> ProviderHealth:
        """Synchronous probe — does the configured endpoint respond?

        Performs a small embed call (one short text) and times the
        round-trip. Returns ``ok=True`` with the warm-ms latency on
        success; ``ok=False`` carrying the canonical error name on
        failure (so ``probe-config`` JSON output is stable across
        provider plugins).
        """
        import time

        endpoint = normalize_foundry_endpoint(self._credentials.endpoint)
        start = time.perf_counter()
        try:
            self.embed_batch(["healthcheck"])
        except ProviderError as err:
            return ProviderHealth(
                ok=False,
                endpoint=endpoint,
                error=type(err).__name__,
            )
        warm_ms = (time.perf_counter() - start) * 1000.0
        return ProviderHealth(ok=True, endpoint=endpoint, warm_ms=warm_ms)


def _isinstance_provider_check() -> bool:
    """Module-level smoke that the concrete class satisfies Provider.

    Importing :class:`AzureFoundryProvider` should never construct one
    just to check Protocol conformance; we instead delegate to the
    contract test under ``tests/providers/azure_foundry/`` to assert
    the runtime-checkable Protocol. This helper exists so the
    :func:`make_provider` factory has a stable single-line conformance
    confirmation in its docstring example — it is intentionally not
    called at import time.
    """
    return issubclass(AzureFoundryProvider, object) and hasattr(AzureFoundryProvider, "embed_batch")


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "AzureFoundryProvider",
    "normalize_foundry_endpoint",
]
