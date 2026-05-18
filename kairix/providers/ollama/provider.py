"""Ollama (local) ``Provider`` implementation.

Translates the universal :class:`kairix.providers.Provider` Protocol
into Ollama's native HTTP wire surface. Ollama is the local-sidecar
LLM runner; an operator runs ``ollama serve`` on ``localhost:11434``
(or names it ``http://ollama:11434`` inside a compose network) and
the plugin talks to it over plain HTTP — no Authorization header, no
api-key, just an open TCP socket on the loopback / overlay network.

Three structural divergences from
:mod:`kairix.providers.openai` / :mod:`kairix.providers.azure_foundry`:

- **No auth header.** Ollama has no credential model — connecting is the
  only "auth". The plugin MUST NOT emit ``Authorization`` or ``api-key``;
  the BDD scenario ``provider_ollama.feature §"no auth header"`` pins
  that absence.
- **Native API shape.** The path is ``/api/embeddings`` (NOT
  ``/v1/embeddings`` like OpenAI, NOT ``/openai/v1/embeddings`` like
  Foundry). Wire body is ``{"model": "<model>", "prompt": "<text>"}``
  for embed and ``{"model": ..., "messages": [...], "stream": false}``
  for chat. The openai SDK does NOT model this surface, so the
  transport-client seam here is a plain ``post(path, json=...)``
  callable instead of the openai-SDK ``embeddings.create`` shape.
- **Single-text embed.** Ollama's ``/api/embeddings`` accepts one
  ``prompt`` per request — there is no batched form on the wire. The
  plugin loops one HTTP request per input text and aggregates the
  responses into the canonical ``list[list[float]]`` shape so the
  Protocol contract (batch in, batch out, same order) is preserved.
  The "batch adapter" responsibility lives here so callers don't have
  to know about Ollama's wire limitation.

DI seams:

- ``credentials``: a :class:`kairix.credentials.Credentials` carrying
  the resolved endpoint and model. ``api_key`` may be the empty string
  for Ollama (the operator has no key to configure) — the plugin
  tolerates that explicitly. The plugin never reads env vars or secrets
  itself (F4 keeps that in ``kairix/paths.py`` / ``kairix/secrets.py``).
- ``transport_client``: a minimal HTTP transport with a single
  ``post(path: str, json: dict) -> dict`` method. Production callers
  leave this ``None`` and the plugin builds an httpx-backed client
  lazily. Tests pass a fake recording transport whose ``recorded_requests``
  list is the wire-shape assertion surface. Allowed-``None`` default
  here because ``Credentials`` is the load-bearing positional and
  ``transport_client`` is a documented test seam; F6 forbids
  ``*_fn=None`` callables-as-test-shims, not all ``=None`` defaults.

Error mapping mirrors the rest of the provider layer (same canonical
vocabulary so the transport-layer retry policy doesn't branch per
plugin), with two Ollama-specific notes:

- **404 model not found** maps to :class:`~kairix.providers.ClientError`
  with the configured model name surfaced — Ollama returns 404 when the
  operator points at a model that hasn't been pulled (``ollama pull
  nomic-embed-text``). 4xx is non-retryable.
- **Connection refused** is the typical failure mode for a stopped
  sidecar; this maps to :class:`~kairix.providers.ProviderUnreachable`
  with the configured endpoint and provider name in the message so the
  operator immediately sees which local URL didn't respond.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from kairix.providers._base import ProviderHealth
from kairix.providers._errors import (
    ClientError,
    ProviderError,
    ProviderUnreachable,
    UpstreamError,
)

if TYPE_CHECKING:
    from kairix.credentials import Credentials

#: Stable plugin name; matches the entry-point key in ``pyproject.toml``
#: and the ``Examples`` row in ``tests/bdd/features/e2e_provider_*.feature``.
PROVIDER_NAME = "ollama"

#: Default embedding dimension fallback. Ollama embed dim depends on
#: the deployed model (``nomic-embed-text`` is 768, ``all-MiniLM`` is
#: 384, etc.) — the plugin records the observed dim from the first
#: embed response and uses that after. ``768`` is a safe pre-embed
#: default because ``nomic-embed-text`` is the most common local
#: embed model paired with Ollama for kairix-style retrieval.
DEFAULT_EMBED_DIMENSION = 768

#: Default chat ``max_tokens`` honoured by the Protocol surface. Ollama
#: maps this to its ``options.num_predict`` field on the wire.
DEFAULT_CHAT_MAX_TOKENS = 800

#: The Ollama-native API path prefix. Distinct from OpenAI's ``/v1/``.
_OLLAMA_API_PREFIX = "/api"

#: The Ollama embeddings endpoint path.
_EMBED_PATH = f"{_OLLAMA_API_PREFIX}/embeddings"

#: The Ollama chat endpoint path.
_CHAT_PATH = f"{_OLLAMA_API_PREFIX}/chat"


@runtime_checkable
class OllamaTransport(Protocol):
    """Minimal HTTP transport surface the plugin consumes.

    A single ``post(path, json)`` method that returns the decoded JSON
    body. Tests pass a fake recording transport; production builds an
    httpx-backed transport lazily. Kept as a Protocol rather than a
    concrete class so the plugin never imports a transport library at
    module scope and the test seam stays type-checked.
    """

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        """POST ``json`` to ``path`` and return the decoded response dict."""


def _status_code_of(err: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from a transport error.

    Mirrors the helper in :mod:`kairix.providers.openai` so the canonical
    error vocabulary stays uniform across plugins. Reads ``.status_code``
    directly (the shape httpx response errors expose) and falls back to
    ``.response.status_code`` (the shape some wrapping exceptions expose).
    Returns ``None`` for plain connection / DNS failures (which the caller
    maps to :class:`ProviderUnreachable`).
    """
    code = getattr(err, "status_code", None)
    if isinstance(code, int):
        return code
    response = getattr(err, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    return None


def _is_connection_failure(err: Exception) -> bool:
    """True for connection-level failures (no HTTP response received).

    Matches stdlib ``ConnectionError`` (and the recording-fake
    ``ConnectionRefusedError`` subclass) plus httpx's ``ConnectError``
    / ``ConnectTimeout`` class-name heuristic. The Ollama failure mode
    of a stopped sidecar surfaces as ``ConnectionRefusedError`` from the
    OS — that's the load-bearing case.
    """
    cls_name = type(err).__name__
    if cls_name in {"ConnectError", "ConnectTimeout", "APIConnectionError", "APITimeoutError"}:
        return True
    if isinstance(err, ConnectionError):
        return True
    return False


def _map_transport_error(err: Exception, *, provider_name: str, endpoint: str, model: str) -> ProviderError:
    """Translate a transport-level exception into a canonical typed error.

    Mapping (same vocabulary as the rest of the provider layer):

    - connection failure → :class:`ProviderUnreachable` carrying the
      configured ``endpoint`` and ``provider_name`` so operators see
      which local URL went unanswered (typical cause: ``ollama serve``
      is not running).
    - HTTP 404 → :class:`ClientError` carrying ``model`` — Ollama
      surfaces 404 when the configured model hasn't been pulled.
    - HTTP 4xx (non-404) → :class:`ClientError` — caller-side problem
      the retry policy can't recover from.
    - HTTP 5xx → :class:`UpstreamError` with ``status_code``.
    - anything else → bare :class:`ProviderError` with ``repr(err)``.

    Connection-failure is checked first because a refused socket has no
    HTTP status to fall through on; the typical Ollama-stopped path
    must produce :class:`ProviderUnreachable` not :class:`ProviderError`.
    """
    if _is_connection_failure(err):
        return ProviderUnreachable(f"Ollama endpoint unreachable for provider {provider_name!r} at {endpoint!r}: {err}")
    status = _status_code_of(err)
    if status == 404:
        return ClientError(
            status,
            f"Ollama model {model!r} not found at {endpoint!r}. "
            f"fix: run `ollama pull {model}` on the sidecar host; "
            f"next: verify with `ollama list` that the model is local",
        )
    if status is not None and 400 <= status < 500:
        return ClientError(status, f"Ollama rejected request for provider {provider_name!r}: {err}")
    if status is not None and status >= 500:
        return UpstreamError(
            f"Ollama upstream error ({status}) for provider {provider_name!r}: {err}",
            status_code=status,
        )
    return ProviderError(f"Ollama transport error for provider {provider_name!r}: {err!r}")


def _normalize_endpoint(endpoint: str) -> str:
    """Strip trailing slashes from the configured endpoint.

    Ollama endpoints are configured as ``http://<host>:<port>`` with no
    URL path; trailing slashes would double up when the plugin appends
    ``/api/embeddings`` to construct the request URL. The normalisation
    is purely defensive — operator config in YAML often picks up a
    trailing slash by accident.
    """
    return endpoint.rstrip("/")


class OllamaProvider:
    """Concrete :class:`kairix.providers.Provider` for a local Ollama sidecar.

    Construction is DI-clean: production passes a resolved
    :class:`kairix.credentials.Credentials` (with ``api_key`` typically
    the empty string) and the plugin builds its own httpx transport
    lazily; tests pass an explicit ``transport_client`` that records
    ``post`` calls.

    The provider satisfies the runtime-checkable Protocol —
    ``isinstance(provider, Provider)`` is True at runtime, which is what
    ``EntryPointRegistry.resolve`` relies on for its return-type
    annotation.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credentials: Credentials,
        transport_client: OllamaTransport | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport_client = transport_client
        # Last-known embed dimension; populated from the first successful
        # embed response so ``dimension()`` reflects what the deployed
        # model actually returned. Falls back to ``credentials.dims`` and
        # then ``DEFAULT_EMBED_DIMENSION`` before any embed has happened.
        self._embed_dimension: int | None = credentials.dims if credentials.dims else None

    # ------------------------------------------------------------------
    # Internal: transport client resolution
    # ------------------------------------------------------------------

    def _client(self) -> OllamaTransport:
        """Return the configured transport client, lazily building one.

        Production callers don't pass ``transport_client``; the plugin
        builds an httpx-backed transport bound to the configured
        endpoint. Tests pass an explicit fake recording client and this
        lazy construction is skipped entirely.

        The lazy import keeps ``import kairix.providers.ollama`` cheap
        for the unit suite — httpx is only imported when production
        code actually opens a socket.
        """
        if self._transport_client is not None:
            return self._transport_client
        return _HttpxOllamaTransport(_normalize_endpoint(self._credentials.endpoint))

    # ------------------------------------------------------------------
    # Provider Protocol
    # ------------------------------------------------------------------

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed N texts via N single-text Ollama round-trips.

        Wire shape (pinned by ``provider_ollama.feature``):

        - URL is ``<endpoint>/api/embeddings`` (NOT ``/v1/embeddings``).
        - Body per request: ``{"model": "<model>", "prompt": "<text>"}``.
        - No ``Authorization``, no ``api-key``, no auth header at all.
        - One HTTP request per text — Ollama has no batched embed wire,
          so the plugin owns the fan-out and aggregation. The Protocol
          contract (batch in, batch out, same order) is preserved.

        Returns one vector per input text, in the same order. Maps any
        transport-level failure to a canonical typed error via
        :func:`_map_transport_error` and re-raises — never returns
        partial / empty vectors silently. Empty input list short-circuits
        without any HTTP traffic.
        """
        if not texts:
            return []
        client = self._client()
        vectors: list[list[float]] = []
        for text in texts:
            try:
                response = client.post(
                    _EMBED_PATH,
                    json={"model": self._credentials.model, "prompt": text},
                )
            except Exception as err:
                raise _map_transport_error(
                    err,
                    provider_name=self.name,
                    endpoint=_normalize_endpoint(self._credentials.endpoint),
                    model=self._credentials.model,
                ) from err
            embedding = list(response.get("embedding", []))
            vectors.append(embedding)
        if vectors and vectors[0]:
            self._embed_dimension = len(vectors[0])
        return vectors

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = DEFAULT_CHAT_MAX_TOKENS,
    ) -> str:
        """Run a single non-streaming chat completion against Ollama.

        Wire shape:

        - URL is ``<endpoint>/api/chat``.
        - Body: ``{"model": ..., "messages": [...], "stream": false,
          "options": {"num_predict": <max_tokens>}}``. ``stream`` is
          forced to false because the Protocol returns a single string,
          not an async iterator.
        - Response: ``{"message": {"role": "assistant", "content":
          "..."}, ...}``.

        Maps transport failures via :func:`_map_transport_error`.
        Returns the assistant content verbatim or ``""`` when Ollama
        produced an empty message (defensive — Ollama models that hit
        ``num_predict=0`` can emit an empty content field).
        """
        client = self._client()
        try:
            response = client.post(
                _CHAT_PATH,
                json={
                    "model": self._credentials.model,
                    "messages": list(messages),
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
        except Exception as err:
            raise _map_transport_error(
                err,
                provider_name=self.name,
                endpoint=_normalize_endpoint(self._credentials.endpoint),
                model=self._credentials.model,
            ) from err
        message = response.get("message", {}) or {}
        content = message.get("content")
        return content if isinstance(content, str) and content else ""

    def dimension(self) -> int:
        """Embedding vector dimension for the configured model.

        Returns the dims captured from the most recent embed response
        when available (matches what the deployed Ollama model actually
        produced — 768 for ``nomic-embed-text``, 384 for
        ``all-MiniLM``, etc.); falls back to ``credentials.dims`` and
        then ``DEFAULT_EMBED_DIMENSION`` so callers always get a
        positive integer.
        """
        if self._embed_dimension:
            return self._embed_dimension
        if self._credentials.dims:
            return self._credentials.dims
        return DEFAULT_EMBED_DIMENSION

    def healthcheck(self) -> ProviderHealth:
        """Synchronous probe — does the configured Ollama sidecar respond?

        Performs a small embed call (one short text) and times the
        round-trip. Returns ``ok=True`` with the warm-ms latency on
        success; ``ok=False`` carrying the canonical error class name on
        failure (so ``probe-config`` JSON output is stable across
        provider plugins). The most common failure here is
        ``ProviderUnreachable`` — the sidecar isn't running.
        """
        import time

        endpoint = _normalize_endpoint(self._credentials.endpoint)
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


class _HttpxOllamaTransport:
    """Production HTTP transport that talks to Ollama via httpx.

    Built lazily by :meth:`OllamaProvider._client` when no test seam is
    injected. Kept module-private because the public DI seam is the
    :class:`OllamaTransport` Protocol, not this concrete class.

    Translates httpx response errors into a uniform shape the error
    mapper consumes:

    - HTTP-status failures raise an exception that carries
      ``.status_code`` (so :func:`_status_code_of` reads it directly).
    - Connection failures propagate as ``ConnectionError`` so
      :func:`_is_connection_failure` recognises them.
    """

    def __init__(self, endpoint: str, *, timeout_s: float = 30.0) -> None:
        self._endpoint = endpoint
        self._timeout_s = timeout_s

    def post(self, path: str, json: dict[str, Any]) -> dict[str, Any]:
        """POST to ``<endpoint><path>`` with no auth header.

        Lazy-imports httpx so the unit suite stays light. Raises a
        ``_HttpStatusError`` carrying ``status_code`` on non-2xx
        responses, or ``ConnectionError`` on socket-level failure.
        """
        import httpx

        url = f"{self._endpoint}{path}"
        try:
            response = httpx.post(url, json=json, timeout=self._timeout_s)
        except httpx.ConnectError as err:
            raise ConnectionError(str(err)) from err
        except httpx.ConnectTimeout as err:
            raise ConnectionError(str(err)) from err
        if response.status_code >= 400:
            raise _HttpStatusError(response.status_code, response.text)
        return dict(response.json())


class _HttpStatusError(Exception):
    """Exception carrying an HTTP ``status_code`` attribute.

    Raised by :class:`_HttpxOllamaTransport.post` on non-2xx responses
    so :func:`_map_transport_error` can read ``status_code`` directly
    via :func:`_status_code_of`. Kept module-private — callers handle
    it via the canonical :class:`ProviderError` hierarchy.
    """

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"HTTP {status_code}: {body[:200]}")


__all__ = [
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_EMBED_DIMENSION",
    "PROVIDER_NAME",
    "OllamaProvider",
    "OllamaTransport",
]
