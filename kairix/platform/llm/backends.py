"""
Concrete LLMBackend implementations.

``AzureOpenAIBackend`` is the historical name for the default LLM backend; it
delegates to the configured provider plugin (resolved from
``kairix.config.yaml``'s ``provider:`` field) rather than directly importing
``kairix._azure``. The class name is retained because many callers across
``kairix.agents``, ``kairix.use_cases`` and ``kairix.platform.setup`` resolve
it through ``get_default_backend()``; renaming would invade those clusters.

Tests substitute the chat/embed callables through ``LLMBackendDeps`` rather
than passing per-method ``*_fn=None`` substitution kwargs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kairix.providers import Provider


def _resolve_provider() -> Provider:
    """Resolve the production ``Provider`` plugin from ``kairix.config.yaml``.

    Reads the ``provider:`` field via :func:`kairix.paths.provider_name` and
    looks up the registered entry-point via :func:`kairix.providers.get_provider`.
    Raises ``ValueError`` if the config field is absent (so operators see a
    typed misconfiguration rather than a silent fall-through).
    """
    from kairix.paths import provider_name
    from kairix.providers import get_provider

    name = provider_name()
    if name is None:
        raise ValueError(
            "kairix.config.yaml is missing the required 'provider:' field. "
            "fix: set 'provider: <plugin-name>' in kairix.config.yaml. "
            "next: see docs/architecture/provider-plugin-architecture.md."
        )
    return get_provider(name)


def _default_chat(messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
    """Production chat callable — delegates to the configured provider plugin.

    Construction route: ``kairix.config.yaml → provider_name() →
    get_provider() → ProviderChatBackend.chat``. Never raises; returns ``""``
    on plugin error (honoured by ``ProviderChatBackend``).
    """
    from kairix.transport.embed_service import ProviderChatBackend

    backend = ProviderChatBackend(_resolve_provider())
    return backend.chat(messages, max_tokens=max_tokens)


def _default_embed(text: str) -> list[float]:
    """Production embed callable — delegates to the configured provider plugin.

    Routes through :class:`kairix.transport.embed_service.ProviderEmbeddingService`
    which owns cache + coalescer wiring. Never raises; returns ``[]`` on
    plugin error.
    """
    from kairix.transport.embed_service import ProviderEmbeddingService

    svc = ProviderEmbeddingService(_resolve_provider())
    return svc.embed(text)


@dataclass
class LLMBackendDeps:
    """Injectable dependencies for ``AzureOpenAIBackend``.

    Each field defaults to a provider-plugin-backed callable; tests construct
    ``LLMBackendDeps(chat=fake_chat, embed=fake_embed)`` rather than passing
    ``chat_fn=`` / ``embed_fn=`` kwargs to the backend's constructor.
    """

    chat: Callable[..., str] = field(default_factory=lambda: _default_chat)
    embed: Callable[[str], list[float]] = field(default_factory=lambda: _default_embed)


class AzureOpenAIBackend:
    """
    Default LLMBackend, backed by the configured ``Provider`` plugin.

    The class name is historical (``Azure`` reflects the original provider);
    the implementation now delegates to whichever plugin is registered in
    ``kairix.config.yaml``'s ``provider:`` field. The defaults route through
    :class:`kairix.transport.embed_service.ProviderChatBackend` /
    :class:`kairix.transport.embed_service.ProviderEmbeddingService` which
    handle the cache + coalescer wiring and honour the ``LLMBackend``
    "never raises" contract.

    # Adapter pattern: satisfies LLMBackend protocol by delegating to the
    # provider plugin via LLMBackendDeps.
    """

    def __init__(self, deps: LLMBackendDeps | None = None) -> None:
        """Construct with optional injectable dependencies for testing.

        Production callers leave ``deps`` ``None`` and the defaults wire
        to the configured provider plugin; tests pass ``LLMBackendDeps(chat=...)``.
        """
        self._deps = deps if deps is not None else LLMBackendDeps()

    def chat(self, messages: list[dict[str, Any]], max_tokens: int = 800) -> str:
        """Chat completion via the configured provider plugin."""
        return self._deps.chat(messages, max_tokens=max_tokens)

    def embed(self, text: str) -> list[float]:
        """Text embedding via the configured provider plugin."""
        return self._deps.embed(text)


class ProviderEvalChatBackend:
    """Adapt a :class:`kairix.providers.Provider` to the eval ``ChatBackend`` protocol.

    Satisfies :class:`kairix.core.protocols.ChatBackend` which exposes
    ``complete(prompt, *, api_key, endpoint, deployment, system, temperature,
    timeout_s) -> str``. The benchmark runner's LLM judge and (post-migration)
    the eval-module query generator consume this protocol; both build a single
    user prompt and want a single string reply.

    The credential kwargs (``api_key``, ``endpoint``, ``deployment``) and the
    tuning kwargs (``temperature``, ``timeout_s``) are accepted for protocol
    conformance but intentionally ignored — the plugin owns its own
    credential-retrieval pattern (Azure → Key Vault, AWS → Secrets Manager,
    etc.) and tuning is configured per-plugin. The ``system`` field is the
    one tuning input that translates: when set, it lands as the leading
    ``system`` message.

    Construction:

      ``provider``: the resolved plugin. Production callers build it via
      :func:`kairix.providers.get_provider(provider_name())`; tests pass a
      ``FakeProvider`` from ``tests/fakes.py``.

    Failure contract: raises on plugin error (matches the legacy
    :class:`kairix._azure.AzureChatBackend` adapter which raised on
    credential-resolution failure). Callers wrap this in their own
    try/except and short-circuit to a 0.0 score when an exception leaks.
    """

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

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
        """Single-prompt completion via the configured provider plugin.

        Builds a messages list and delegates to ``Provider.chat``. The
        credential / tuning kwargs are accepted for ChatBackend protocol
        conformance but intentionally not propagated (the plugin owns its
        own credential pattern).
        """
        # Credential and tuning kwargs are protocol-conformance surface
        # that this adapter drops — the plugin owns its own credential-
        # retrieval pattern (Azure → Key Vault, AWS → Secrets Manager,
        # etc.) and tuning configuration. The ``_unused`` tuple consumes
        # the names as Load references so F19 (S1172) sees them as used
        # while making the intent explicit to a reader.
        _unused = (api_key, endpoint, deployment, temperature, timeout_s)
        del _unused
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self._provider.chat(messages)
