"""Anthropic ``Provider`` implementation — chat-only via the Messages API.

Translates the universal :class:`kairix.providers.Provider` Protocol
into Anthropic's wire surface (``POST /v1/messages`` on
``api.anthropic.com``). Anthropic is the chat-only family in the
provider matrix — :meth:`AnthropicProvider.embed_batch` raises
:class:`kairix.providers.EmbedNotSupported` immediately, before any
outbound request is constructed, because Anthropic ships no embeddings
endpoint at all. Operators wanting embed alongside Anthropic chat
combine ``anthropic`` (chat) with a separate embed provider
(typically ``openai``).

Wire shape Anthropic pins (distinct from every other plugin in the
matrix):

- Auth header is ``x-api-key: <api_key>`` — **not** the
  ``Authorization: Bearer ...`` shape used by OpenAI / LiteLLM, and
  **not** the ``api-key: ...`` shape used by AzureOpenAI / Foundry.
- A second mandatory header ``anthropic-version: 2023-06-01`` declares
  the wire-format version Anthropic should serve. Pinned here as
  :data:`ANTHROPIC_API_VERSION` so the version moves in lockstep with
  any wire-format upgrade in a single commit.
- Request body: ``{"model": "<id>", "max_tokens": N, "messages": [...]}``.
  Anthropic requires ``max_tokens`` on every request (no implicit
  default like OpenAI) so the plugin honours the Protocol's
  ``max_tokens=`` kwarg verbatim with a sensible default.
- Response body: ``{"content": [{"type": "text", "text": "..."}], ...}``
  — content is an *array* of blocks, not a single string. This plugin
  joins all text blocks (skipping non-text blocks like tool-use) so
  callers see the same ``str`` return type as every other plugin's
  ``chat()``.

Error mapping mirrors the rest of the provider layer (same canonical
typed-error vocabulary so the transport-layer retry policy doesn't
branch per plugin):

- HTTP 429 → :class:`RateLimited` (with ``retry-after`` parsed)
- HTTP 401 / 403 → :class:`AuthError`
- HTTP 400 → :class:`ClientError` (bad model id / payload)
- HTTP 5xx → :class:`UpstreamError`
- connection failure → :class:`ProviderUnreachable`
- anything else → bare :class:`ProviderError`

DI seams:

- ``credentials``: a :class:`kairix.credentials.Credentials` carrying
  the resolved api-key / endpoint / model. The plugin never reads env
  vars or secrets itself (F4 keeps that in
  ``kairix/paths.py`` / ``kairix/secrets.py``).
- ``transport_client``: a recording fake mirroring the official
  ``anthropic`` Python SDK surface (``client.messages.create(...)``).
  Production callers leave this ``None`` and the plugin lazily resolves
  one via :func:`_build_default_client` (which imports the official
  SDK on demand so the dependency is optional at import time). Tests
  pass a recording fake. Allowed-``None`` here because ``Credentials``
  is the load-bearing positional and ``transport_client`` is a
  documented test seam; F6 forbids ``*_fn=None`` callables-as-test-shims,
  not all ``=None`` defaults.

See ``docs/architecture/provider-plugin-architecture.md`` for the
ADR and ``tests/bdd/features/provider_anthropic.feature`` for the
wire-shape contract this plugin pins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kairix.providers._base import ProviderHealth
from kairix.providers._errors import (
    AuthError,
    ClientError,
    EmbedNotSupported,
    ProviderError,
    ProviderUnreachable,
    RateLimited,
    UpstreamError,
)

if TYPE_CHECKING:
    from kairix.credentials import Credentials

#: Stable plugin name; matches the entry-point key in ``pyproject.toml``
#: and the ``Examples`` row in ``tests/bdd/features/e2e_provider_*.feature``.
PROVIDER_NAME = "anthropic"

#: Default Anthropic API endpoint. Operators rarely override (Anthropic
#: doesn't offer regional or self-hosted variants the way Azure / Bedrock
#: do) but the value remains driven by ``credentials.endpoint`` so a
#: future enterprise tier with a different host flows through the same
#: code path with no change.
DEFAULT_ENDPOINT = "https://api.anthropic.com"

#: The ``anthropic-version`` header value Anthropic requires on every
#: request. Pinning as a module-level constant means any version bump
#: lands as a single one-line commit instead of being spread across
#: production code and the BDD/unit test layer. Anthropic supports
#: multiple versions concurrently; ``2023-06-01`` is the GA Messages-API
#: version every Claude 3 / 3.5 / 3.7 model supports.
ANTHROPIC_API_VERSION = "2023-06-01"

#: Default chat ``max_tokens`` honoured by the Protocol surface. Anthropic
#: requires this on every request (unlike OpenAI which defaults
#: server-side) so the plugin must always send it.
DEFAULT_CHAT_MAX_TOKENS = 800

#: Anthropic does not ship an embeddings endpoint, so :meth:`dimension`
#: has no meaningful value to return. We surface ``0`` so callers that
#: assume a positive integer trip an obvious invariant (the indexing
#: layer rejects 0-dim vectors loudly) rather than silently writing a
#: nonsense dimension into the vector store.
EMBED_DIMENSION_NOT_APPLICABLE = 0

#: Default request timeout (seconds) when production builds the
#: transport client via the official SDK. Tests pass an explicit fake
#: client and skip this knob entirely.
_DEFAULT_TIMEOUT_S = 30.0


def _status_code_of(err: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from a transport error.

    The official ``anthropic`` SDK attaches ``.status_code`` to
    ``APIStatusError`` and its subclasses (``RateLimitError`` is 429,
    ``AuthenticationError`` is 401, etc.) — identical to the openai
    SDK's exception surface. httpx response errors carry
    ``.response.status_code``. Returns ``None`` for plain connection /
    DNS failures (which the caller maps to :class:`ProviderUnreachable`).
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
    the Anthropic / openai SDKs put on the wire today).
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

    Matches the anthropic SDK's ``APIConnectionError`` and the stdlib
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

    Mapping (identical vocabulary to the other plugins so the
    transport-layer retry policy doesn't branch per plugin):

    - ``status_code == 429`` → :class:`RateLimited` (carries Retry-After hint)
    - ``status_code in (401, 403)`` → :class:`AuthError`
    - ``status_code == 400`` → :class:`ClientError` (non-retryable 4xx)
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
            f"Anthropic rate-limited (429) for provider {provider_name!r}: {err}",
            retry_after_s=_retry_after_of(err),
        )
    if status in (401, 403):
        return AuthError(f"Anthropic auth rejected ({status}) for provider {provider_name!r}: {err}")
    if status == 400:
        return ClientError(status, f"Anthropic client error for provider {provider_name!r}: {err}")
    if status is not None and status >= 500:
        return UpstreamError(
            f"Anthropic upstream error ({status}) for provider {provider_name!r}: {err}",
            status_code=status,
        )
    if _is_connection_failure(err):
        return ProviderUnreachable(f"Anthropic endpoint unreachable for provider {provider_name!r}: {err}")
    return ProviderError(f"Anthropic transport error for provider {provider_name!r}: {err!r}")


def _extract_text_from_content_blocks(content: Any) -> str:
    """Join the ``text`` field of every text block in an Anthropic response.

    Anthropic's Messages API returns ``content`` as a *list* of typed
    blocks (``{"type": "text", "text": "..."}`` is the common case;
    ``{"type": "tool_use", ...}`` blocks are also possible when the
    model decides to call a tool). The Protocol's ``chat()`` returns a
    plain ``str``, so this helper concatenates the text of every text
    block in order and silently drops non-text blocks — matching the
    behaviour callers expect from every other plugin's ``chat()``.

    Returns ``""`` when ``content`` is missing, empty, or contains no
    text blocks; mirrors the ``return content or ""`` shape the
    azure_foundry / openai plugins use for None-content responses.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        # Some test fakes return content as a plain string for brevity;
        # accepting it here keeps the fake surface ergonomic.
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for block in content:
        block_type = _block_attr(block, "type")
        if block_type != "text":
            continue
        text = _block_attr(block, "text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _block_attr(block: Any, name: str) -> Any:
    """Read ``name`` off a content block, tolerating dict-or-object shape.

    The official Anthropic SDK returns Pydantic model instances
    (attribute access: ``block.type``, ``block.text``). The raw JSON
    surface — and the test fakes — use dicts (key access:
    ``block["type"]``). Tolerating both lets the same impl power
    production *and* tests without a separate parsing branch.
    """
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


class AnthropicProvider:
    """Concrete :class:`kairix.providers.Provider` for Anthropic (chat-only).

    Construction is DI-clean: production passes a resolved
    :class:`kairix.credentials.Credentials` and lets the plugin build
    its own transport client lazily; tests pass an explicit
    ``transport_client`` that records ``messages.create`` calls and
    captures the headers the plugin actually sent.

    The provider satisfies the runtime-checkable Protocol —
    ``isinstance(provider, Provider)`` is True at runtime, which is
    what ``EntryPointRegistry.resolve`` relies on for its return-type
    annotation. ``embed_batch`` is present (Protocol-required) but
    short-circuits to :class:`EmbedNotSupported` before any transport
    call — the load-bearing invariant tested explicitly in
    ``tests/providers/anthropic/test_provider.py``.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credentials: Credentials,
        transport_client: Any | None = None,
    ) -> None:
        self._credentials = credentials
        self._transport_client = transport_client

    # ------------------------------------------------------------------
    # Internal: transport client resolution
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the configured transport client, lazily building one.

        Production callers don't pass ``transport_client``; the plugin
        builds an Anthropic SDK client via :func:`_build_default_client`
        which imports the official ``anthropic`` SDK on demand. Tests
        pass an explicit fake recording client and this lazy
        construction is skipped entirely.
        """
        if self._transport_client is not None:
            return self._transport_client
        return _build_default_client(
            api_key=self._credentials.api_key,
            endpoint=self._credentials.endpoint or DEFAULT_ENDPOINT,
            timeout_s=_DEFAULT_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Provider Protocol
    # ------------------------------------------------------------------

    def embed_batch(self, _texts: list[str]) -> list[list[float]]:
        """Always raise :class:`EmbedNotSupported` — Anthropic ships no embed surface.

        Short-circuits **before** any transport client is resolved or
        any outbound request is constructed. This is a hard invariant:
        no Anthropic call should ever reach the network for an embed
        operation, because Anthropic doesn't have an embed endpoint to
        receive it. The test
        ``test_embed_short_circuits_before_any_network_call`` pins the
        invariant by asserting that the recording transport's
        ``messages.create`` was never invoked.

        The ``_texts`` parameter is ``_``-prefixed (F19) because the
        Protocol position is load-bearing but this implementation
        never reads it — the typed error fires regardless of input.

        Operators wanting embed alongside Anthropic chat configure a
        separate embed provider (typically ``openai``); the error
        message names ``anthropic`` and points at the alternative.
        """
        raise EmbedNotSupported(provider_name=self.name)

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = DEFAULT_CHAT_MAX_TOKENS,
    ) -> str:
        """Run a single chat completion against Anthropic's Messages API.

        Wire shape (pinned by ``provider_anthropic.feature``):

        - Request goes to ``POST /v1/messages`` on the configured host;
        - the auth header is ``x-api-key: <api_key>`` — **not** the
          ``Authorization: Bearer ...`` shape used by OpenAI;
        - the ``anthropic-version`` header carries
          :data:`ANTHROPIC_API_VERSION` so Anthropic serves the
          declared wire format;
        - request body is ``{"model": ..., "max_tokens": ..., "messages": ...}``.

        Translates the Protocol's ``messages=[...]`` shape (which uses
        kairix's universal ``role``/``content`` schema) directly into
        the SDK call. Anthropic accepts the same ``role``/``content``
        keys, so the message list passes through verbatim. Response
        ``content`` is an *array* of typed blocks; we join the text
        blocks in order (see :func:`_extract_text_from_content_blocks`).
        Maps transport failures via :func:`_map_transport_error`.
        """
        client = self._client()
        try:
            response = client.messages.create(
                model=self._credentials.model,
                max_tokens=max_tokens,
                messages=list(messages),
            )
        except Exception as err:
            raise _map_transport_error(err, provider_name=self.name) from err
        content = getattr(response, "content", None)
        if content is None and isinstance(response, dict):
            content = response.get("content")
        return _extract_text_from_content_blocks(content)

    def dimension(self) -> int:
        """Return ``0`` — Anthropic has no embedding surface.

        Anthropic ships chat-only; there is no embedding model whose
        vector dimension we could meaningfully return. Surfacing ``0``
        means callers that assume a positive integer trip an obvious
        invariant (the indexing layer rejects 0-dim vectors loudly)
        rather than silently writing a nonsense dimension into the
        vector store. Pair ``anthropic`` with ``openai`` (or any other
        embed-capable plugin) for embed workloads.
        """
        return EMBED_DIMENSION_NOT_APPLICABLE

    def healthcheck(self) -> ProviderHealth:
        """Synchronous probe — does the configured endpoint respond?

        Performs a tiny chat call (one short user message, single
        token) and times the round-trip. Returns ``ok=True`` with the
        warm-ms latency on success; ``ok=False`` carrying the canonical
        error name on failure (so ``probe-config`` JSON output is
        stable across provider plugins). Uses ``chat`` (not
        ``embed_batch``) because Anthropic is chat-only — using embed
        would always report ``EmbedNotSupported`` even on a perfectly
        healthy endpoint.
        """
        import time

        endpoint = self._credentials.endpoint or DEFAULT_ENDPOINT
        start = time.perf_counter()
        try:
            self.chat(
                [{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except ProviderError as err:
            return ProviderHealth(
                ok=False,
                endpoint=endpoint,
                error=type(err).__name__,
            )
        warm_ms = (time.perf_counter() - start) * 1000.0
        return ProviderHealth(ok=True, endpoint=endpoint, warm_ms=warm_ms)


def _build_default_client(*, api_key: str, endpoint: str, timeout_s: float) -> Any:
    """Construct an official ``anthropic`` SDK client.

    Imported on demand (rather than at module import time) so the
    ``anthropic`` dependency is only required when the plugin is
    actually selected by an operator. Other plugins (azure_foundry /
    openai / bedrock) take the same lazy-import approach for their
    respective SDKs.

    The SDK reads its API version from the ``default_headers`` kwarg —
    we set ``anthropic-version`` explicitly so the value lives in this
    module (single source of truth) rather than relying on whatever
    default the installed SDK version ships with.
    """
    # The ``anthropic`` SDK is an optional dependency — only required
    # when an operator actually selects KAIRIX_PROVIDER=anthropic.
    # mypy is configured with ``ignore_missing_imports = true`` so the
    # missing distribution doesn't fail static analysis; at runtime the
    # ModuleNotFoundError surfaces a clear "pip install anthropic"
    # message via the ImportError below.
    try:
        from anthropic import Anthropic
    except ModuleNotFoundError as err:
        raise RuntimeError(
            "anthropic provider selected but the 'anthropic' Python SDK is not installed. "
            "fix: pip install anthropic; "
            "next: re-run with KAIRIX_PROVIDER=anthropic once the SDK is available."
        ) from err

    return Anthropic(
        api_key=api_key,
        base_url=endpoint or DEFAULT_ENDPOINT,
        default_headers={"anthropic-version": ANTHROPIC_API_VERSION},
        timeout=timeout_s,
    )


__all__ = [
    "ANTHROPIC_API_VERSION",
    "DEFAULT_CHAT_MAX_TOKENS",
    "DEFAULT_ENDPOINT",
    "EMBED_DIMENSION_NOT_APPLICABLE",
    "PROVIDER_NAME",
    "AnthropicProvider",
]
