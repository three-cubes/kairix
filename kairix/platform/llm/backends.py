"""
Concrete LLMBackend implementations.

``AzureOpenAIBackend`` is the historical name for the default LLM backend; it
delegates to the configured provider plugin (resolved from
``kairix.config.yaml``'s ``provider:`` field). The class name is retained
because many callers across ``kairix.agents``, ``kairix.use_cases`` and
``kairix.platform.setup`` resolve it through ``get_default_backend()``;
renaming would invade those clusters.

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
