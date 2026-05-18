"""OpenAI-direct ``Provider`` implementation.

Translates the universal :class:`kairix.providers.Provider` Protocol
into the OpenAI-direct wire surface (api.openai.com or any drop-in
OpenAI-compatible base URL — Together, Groq, Fireworks, local vLLM
servers, etc).

Structurally the simplest plugin: an OpenAI client built against the
configured ``base_url`` with ``Authorization: Bearer <api_key>`` auth.
Compared to :mod:`kairix.providers.azure_foundry` this plugin:

- does NOT append ``/openai/v1`` to the endpoint (OpenAI-direct
  callers configure the full base URL themselves, typically
  ``https://api.openai.com/v1``);
- uses ``Bearer`` token auth (the openai SDK's default for the
  ``OpenAI`` client class), distinct from the ``api-key`` header used
  by ``AzureOpenAI`` against the legacy / Foundry endpoints;
- carries the configured model name through verbatim
  (``text-embedding-3-large`` etc) — no deployment-name translation.

That structural simplicity is the point: this plugin is the proof of
shape that the Protocol-and-error-mapping pattern from
:mod:`kairix.providers.azure_foundry` carries over to a non-Azure
endpoint without surgery. Once this is green, third-party plugins
under ``[project.entry-points."kairix.providers"]`` follow the same
shell.

DI seams:

- ``credentials``: a :class:`kairix.credentials.Credentials` carrying
  the resolved api-key / endpoint / model. The plugin never reads env
  vars or secrets itself (F4 keeps that in
  ``kairix/paths.py`` / ``kairix/secrets.py``).
- ``transport_client``: an OpenAI-compatible client. Production callers
  leave this ``None`` and the plugin resolves the process-shared client
  via :func:`kairix.transport.pool.get_client` so every coalescer batch
  dispatch reuses the same ``httpx.Client`` connection pool. Tests pass
  a fake recording client. Allowed-``None`` here because ``Credentials``
  is the load-bearing positional and ``transport_client`` is a
  documented test seam; F6 forbids ``*_fn=None`` callables-as-test-shims,
  not all ``=None`` defaults.

Error mapping is delegated to private helpers that mirror the
azure_foundry shape (same 429/401/403/5xx/connection branches, same
``Retry-After`` parsing) — both plugins consume the openai SDK and
the SDK raises the same exception classes regardless of which endpoint
family is on the other side.
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
PROVIDER_NAME = "openai"

#: Default embedding dimension. Matches the dimension produced by
#: ``text-embedding-3-large`` (and the kairix-wide
#: ``KAIRIX_EMBED_DIMS`` / ``kairix.core.db.EMBED_VECTOR_DIMS`` default)
#: so callers querying ``dimension()`` before any embed has happened
#: still get a positive integer compatible with the indexing layer.
DEFAULT_EMBED_DIMENSION = 1536

#: Default chat ``max_tokens`` honoured by the Protocol surface.
DEFAULT_CHAT_MAX_TOKENS = 800

#: Default request timeout (seconds) when production builds the
#: transport client via ``kairix.transport.pool.get_client``. Tests pass
#: an explicit fake client and skip this knob entirely.
_DEFAULT_TIMEOUT_S = 30.0


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
    the openai SDK puts on the wire today).
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
    ``ConnectionError`` family; test seams raise bare ``ConnectionError``
    to stand in.
    """
    cls_name = type(err).__name__
    if cls_name in {"APIConnectionError", "APITimeoutError"}:
        return True
    if isinstance(err, ConnectionError):
        return True
    return False


def _map_transport_error(err: Exception, *, provider_name: str) -> ProviderError:
    """Translate a transport-level exception into a canonical typed error.

    Mapping (identical vocabulary to :mod:`kairix.providers.azure_foundry`
    so the transport-layer retry policy doesn't branch per plugin):

    - ``status_code == 429`` → :class:`RateLimited` (carries Retry-After hint)
    - ``status_code in (401, 403)`` → :class:`AuthError`
    - ``status_code >= 500`` → :class:`UpstreamError`
    - connection failure → :class:`ProviderUnreachable`
    - anything else → bare :class:`ProviderError` with ``repr(err)``

    The ``provider_name`` is interpolated into the surfaced messages so
    operators see which plugin failed when multiple plugins are wired
    (e.g. ``openai`` for embed, ``anthropic`` for chat).
    """
    status = _status_code_of(err)
    if status == 429:
        return RateLimited(
            f"OpenAI rate-limited (429): {err}",
            retry_after_s=_retry_after_of(err),
        )
    if status in (401, 403):
        return AuthError(f"OpenAI auth rejected ({status}) for provider {provider_name!r}: {err}")
    if status is not None and status >= 500:
        return UpstreamError(
            f"OpenAI upstream error ({status}): {err}",
            status_code=status,
        )
    if _is_connection_failure(err):
        return ProviderUnreachable(f"OpenAI endpoint unreachable for provider {provider_name!r}: {err}")
    return ProviderError(f"OpenAI transport error: {err!r}")


class OpenAIProvider:
    """Concrete :class:`kairix.providers.Provider` for OpenAI-direct.

    Construction is DI-clean: production passes a resolved
    :class:`kairix.credentials.Credentials` and lets the plugin resolve
    its transport client lazily via the process-shared
    :func:`kairix.transport.pool.get_client`; tests pass an explicit
    ``transport_client`` that records ``embeddings.create`` /
    ``chat.completions.create`` calls.

    The provider satisfies the runtime-checkable Protocol —
    ``isinstance(provider, Provider)`` is True at runtime, which is what
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
        """Return the configured transport client, lazily resolving one.

        Production callers don't pass ``transport_client``; the plugin
        resolves the process-shared client via
        :func:`kairix.transport.pool.get_client` so every coalescer
        batch dispatch reuses the same ``httpx.Client`` connection pool
        (paying one TLS handshake per process, not one per batch).
        Tests pass an explicit fake recording client and this lazy
        resolution is skipped entirely.
        """
        if self._transport_client is not None:
            return self._transport_client
        from kairix.transport.pool import get_client

        return get_client(
            self._credentials.api_key,
            self._credentials.endpoint,
            _DEFAULT_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Provider Protocol
    # ------------------------------------------------------------------

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts in one OpenAI round-trip.

        Wire shape (pinned by ``provider_openai.feature``):

        - URL base comes from ``credentials.endpoint`` verbatim (no
          ``/openai/v1`` suffix logic — OpenAI-direct operators
          configure the full base URL, typically
          ``https://api.openai.com/v1``);
        - request path ends with ``/embeddings``;
        - ``model=`` carries the configured model name verbatim
          (e.g. ``text-embedding-3-large``);
        - the auth header is ``Authorization: Bearer <api_key>`` — the
          openai SDK's default for ``OpenAI`` clients (distinct from the
          ``api-key`` header used by ``AzureOpenAI`` / Foundry).

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
        """Run a single chat completion against OpenAI.

        Translates the Protocol's ``messages=[...]`` shape into the
        SDK's ``chat.completions.create(model=, messages=, max_tokens=)``
        call. ``model=`` carries the configured model name verbatim,
        ``temperature`` defaults to ``0.3`` matching the rest of kairix
        (deterministic-ish synthesis). Maps transport failures via
        :func:`_map_transport_error`.
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
        """Embedding vector dimension for the configured model.

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

        endpoint = self._credentials.endpoint
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


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "OpenAIProvider",
]
