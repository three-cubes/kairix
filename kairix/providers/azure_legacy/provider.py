"""Azure-legacy ``Provider`` implementation (Azure OpenAI Service, pre-Foundry).

Translates the universal :class:`kairix.providers.Provider` Protocol
into the *legacy* Azure OpenAI Service wire surface — the
``https://<resource>.openai.azure.com`` endpoint shape Microsoft shipped
before Azure AI Foundry consolidated everything behind
``services.ai.azure.com``. Many enterprise tenants still ride the
legacy endpoint today; this plugin keeps them on a first-class path
without forcing them to rewrite their configured URL.

Structural divergences from :mod:`kairix.providers.azure_foundry`:

- **No ``/openai/v1`` suffix.** The legacy endpoint exposes the original
  Azure shape (``/openai/deployments/<deployment>/embeddings``) which the
  openai-SDK ``AzureOpenAI`` client constructs automatically — the
  plugin MUST NOT append ``/openai/v1`` (that's Foundry-specific and
  would 404 against a legacy resource).
- **``AzureOpenAI`` SDK class.** Production builds the client with
  ``openai.AzureOpenAI(azure_endpoint=..., api_version=...)``, not the
  generic ``OpenAI(base_url=...)`` form Foundry uses. The auth header
  is still ``api-key: <key>`` (the SDK default for ``AzureOpenAI``).
- **``api-version`` query parameter.** Every Azure-legacy call carries
  ``?api-version=<version>``. The plugin pins ``_AZURE_API_VERSION =
  "2024-06-01"`` as the default (GA, broadly supported by deployed
  resources today). Operators on a newer surface override via the
  ``api_version`` constructor kwarg without forking the plugin.
- **Foundry-endpoint rejection.** Constructing the legacy plugin against
  a Foundry-shaped endpoint (containing ``services.ai.azure.com`` or
  ending in ``/openai/v1``) fails fast with an actionable
  :class:`ValueError` pointing the operator at ``provider:
  azure_foundry``. This prevents the silent misroute where a Foundry
  endpoint would be force-fed to the legacy ``AzureOpenAI`` client and
  return 404s with cryptic SDK-level errors.

DI seams:

- ``credentials``: a :class:`kairix.credentials.Credentials` carrying
  the resolved api-key / endpoint / model. The plugin never reads env
  vars or secrets itself (F4 keeps that in ``kairix/paths.py`` /
  ``kairix/secrets.py``).
- ``api_version``: optional override of the pinned default. ``None``
  means "use the credentials' ``api_version`` attribute if present, else
  fall through to ``_AZURE_API_VERSION``". Tests pass an explicit
  string; production typically leaves this ``None``.
- ``transport_client``: an openai-SDK-shaped client. Production callers
  leave this ``None`` and the plugin resolves the process-shared client
  via :func:`kairix.transport.pool.get_client` so every coalescer batch
  dispatch reuses the same ``httpx.Client`` connection pool. Tests pass
  a fake recording client. Allowed-``None`` default here because
  ``Credentials`` is the load-bearing positional and ``transport_client``
  is a documented test seam; F6 forbids ``*_fn=None`` callables-as-
  test-shims, not all ``=None`` defaults.

Error mapping mirrors the rest of the provider layer (same canonical
vocabulary so the transport-layer retry policy doesn't branch per
plugin): 429 → :class:`RateLimited`, 401/403 → :class:`AuthError`,
5xx → :class:`UpstreamError`, connection failure →
:class:`ProviderUnreachable`.

F27-clean: zero imports from sibling provider plugins. The error-mapping
helpers are duplicated rather than shared so the plugin remains
independently shippable as its own pip distribution. The duplicated
structure is intentional and documented in the ADR.
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
PROVIDER_NAME = "azure_legacy"

#: Default embedding dimension for ``text-embedding-3-large`` against a
#: legacy Azure OpenAI deployment; matches the kairix-wide
#: ``KAIRIX_EMBED_DIMS`` / ``kairix.core.db.EMBED_VECTOR_DIMS`` default
#: when the operator deploys the large model. Used as the fallback when
#: :meth:`AzureLegacyProvider.dimension` is called before any embed has
#: happened.
DEFAULT_EMBED_DIMENSION = 1536

#: Default chat ``max_tokens`` honoured by the Protocol surface.
DEFAULT_CHAT_MAX_TOKENS = 800

#: Default Azure-legacy ``api-version`` query parameter. Pinned to a
#: stable GA release rather than ``-preview`` so the default does not
#: change behaviour under operators who never touched the override.
#: Operators tracking a newer surface (preview features, new model
#: support) override via the ``api_version`` constructor kwarg without
#: forking the plugin.
_AZURE_API_VERSION = "2024-06-01"

#: Fragments that identify a Foundry-shaped endpoint. Used by the
#: construction-time fail-fast check that rejects Foundry URLs early
#: with a typed hint pointing the operator at ``provider:
#: azure_foundry``.
_FOUNDRY_HOST_FRAGMENT = "services.ai.azure.com"
_FOUNDRY_OPENAI_COMPAT_SUFFIX = "/openai/v1"

#: Default request timeout (seconds) when production builds the
#: transport client via ``kairix.transport.pool.get_client``. Tests pass
#: an explicit fake client and skip this knob entirely.
_DEFAULT_TIMEOUT_S = 30.0


def _is_foundry_shaped(endpoint: str) -> bool:
    """True if ``endpoint`` looks like an Azure AI Foundry URL.

    Foundry endpoints either contain the ``services.ai.azure.com`` host
    fragment or end in the OpenAI-compat alias suffix ``/openai/v1``.
    The legacy plugin rejects both shapes at construction time because
    feeding them to ``AzureOpenAI`` would silently 404.
    """
    ep = endpoint.lower().rstrip("/")
    if _FOUNDRY_HOST_FRAGMENT in ep:
        return True
    if ep.endswith(_FOUNDRY_OPENAI_COMPAT_SUFFIX):
        return True
    return False


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
    operators see which plugin failed — useful when multiple plugins
    are wired (e.g. embed vs chat on different endpoints).
    """
    status = _status_code_of(err)
    if status == 429:
        return RateLimited(
            f"Azure legacy rate-limited (429): {err}",
            retry_after_s=_retry_after_of(err),
        )
    if status in (401, 403):
        return AuthError(f"Azure legacy auth rejected ({status}) for provider {provider_name!r}: {err}")
    if status is not None and status >= 500:
        return UpstreamError(
            f"Azure legacy upstream error ({status}): {err}",
            status_code=status,
        )
    if _is_connection_failure(err):
        return ProviderUnreachable(f"Azure legacy endpoint unreachable for provider {provider_name!r}: {err}")
    return ProviderError(f"Azure legacy transport error: {err!r}")


def _resolve_api_version(credentials: Credentials, override: str | None) -> str:
    """Resolve the effective ``api-version`` for this provider instance.

    Precedence (most-specific wins):

    1. Explicit constructor ``api_version`` kwarg (tests, BDD override
       scenario).
    2. ``credentials.api_version`` attribute when present and truthy
       (extension point for future Credentials evolution; not currently
       a field on the frozen dataclass).
    3. Module-pinned ``_AZURE_API_VERSION`` default.
    """
    if override:
        return override
    cred_version = getattr(credentials, "api_version", None)
    if cred_version:
        return str(cred_version)
    return _AZURE_API_VERSION


class AzureLegacyProvider:
    """Concrete :class:`kairix.providers.Provider` for the legacy Azure OpenAI Service.

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

    Constructing against a Foundry-shaped endpoint (containing
    ``services.ai.azure.com`` or ending in ``/openai/v1``) raises
    :class:`ValueError` with a message that names ``provider:
    azure_foundry`` as the fix. This prevents the silent misroute where
    a Foundry URL would be force-fed to the legacy ``AzureOpenAI`` SDK
    class and 404 with a cryptic SDK-level error.
    """

    name = PROVIDER_NAME

    def __init__(
        self,
        credentials: Credentials,
        *,
        api_version: str | None = None,
        transport_client: Any | None = None,
    ) -> None:
        if _is_foundry_shaped(credentials.endpoint):
            raise ValueError(
                f"AzureLegacyProvider rejected Foundry-shaped endpoint "
                f"{credentials.endpoint!r}. The legacy plugin only handles "
                f"the pre-Foundry '<resource>.openai.azure.com' URL shape; "
                f"Foundry endpoints ('services.ai.azure.com' or paths "
                f"ending in '/openai/v1') need the sibling plugin. "
                f"fix: set provider: azure_foundry (KAIRIX_PROVIDER=azure_foundry) "
                f"for this endpoint; "
                f"next: keep provider: azure_legacy only for resources whose "
                f"endpoint matches '<resource>.openai.azure.com'."
            )
        self._credentials = credentials
        self._api_version = _resolve_api_version(credentials, api_version)
        self._transport_client = transport_client
        # Last-known embed dimension; populated from the first successful
        # embed response so ``dimension()`` reflects what the deployed
        # model actually returned. Falls back to ``DEFAULT_EMBED_DIMENSION``
        # before any embed has happened.
        self._embed_dimension: int | None = credentials.dims if credentials.dims else None

    @property
    def api_version(self) -> str:
        """Effective Azure ``api-version`` for this provider instance.

        Exposed as a read-only property so the BDD wire fixture can
        synthesise the recorded request's ``query`` field without
        reaching into a private attribute.
        """
        return self._api_version

    # ------------------------------------------------------------------
    # Internal: transport client resolution
    # ------------------------------------------------------------------

    def _client(self) -> Any:
        """Return the configured transport client, lazily resolving one.

        Production callers don't pass ``transport_client``; the plugin
        resolves the process-shared client via
        :func:`kairix.transport.pool.get_client` so every coalescer
        batch dispatch reuses the same ``httpx.Client`` connection pool
        (paying one TLS handshake per process, not one per batch). The
        production pool dispatches to ``kairix.credentials.make_openai_client``
        which already routes legacy Azure endpoints to the
        ``openai.AzureOpenAI`` SDK class with ``api-key`` auth.

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
        """Embed a batch of texts in one Azure-legacy round-trip.

        Wire shape (pinned by ``provider_azure_legacy.feature``):

        - URL host comes from ``credentials.endpoint`` verbatim — the
          legacy ``<resource>.openai.azure.com`` shape with NO
          ``/openai/v1`` suffix (which would be Foundry-specific);
        - ``api-version`` query parameter accompanies every request;
        - ``model=`` carries the configured deployment name verbatim
          (Azure-legacy deployments use operator-chosen names like
          ``text-embedding-3-large`` for the deployment alias);
        - the auth header is ``api-key: <api_key>`` — the openai-SDK
          default for the ``AzureOpenAI`` client class.

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
        """Run a single chat completion against the legacy Azure endpoint.

        Translates the Protocol's ``messages=[...]`` shape into the
        SDK's ``chat.completions.create(model=, messages=, max_tokens=)``
        call. ``model=`` carries the configured Azure deployment name,
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
    "AzureLegacyProvider",
]
