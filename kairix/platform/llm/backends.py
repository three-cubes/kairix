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

from kairix.paths import provider_name as _config_provider_name
from kairix.providers import get_provider as _registry_get_provider


def resolve_provider(
    *,
    provider_name_fn: Callable[[], str | None] = _config_provider_name,
    get_provider_fn: Callable[..., Any] = _registry_get_provider,
) -> Provider:
    """Resolve the production ``Provider`` plugin from ``kairix.config.yaml``.

    Reads the ``provider:`` field via ``provider_name_fn`` (defaults to
    :func:`kairix.paths.provider_name`) and looks up the registered
    entry-point via ``get_provider_fn`` (defaults to
    :func:`kairix.providers.get_provider`). Raises ``ValueError`` if
    the config field is absent.

    Tests pass ``provider_name_fn`` / ``get_provider_fn`` to inject
    the resolution path — F1-clean (no internal patching) and F6-clean
    (production defaults are real callables, not ``None``).
    """
    name = provider_name_fn()
    if name is None:
        raise ValueError(
            "kairix.config.yaml is missing the required 'provider:' field. "
            "fix: set 'provider: <plugin-name>' in kairix.config.yaml. "
            "next: see docs/architecture/provider-plugin-architecture.md."
        )
    provider: Provider = get_provider_fn(name)
    return provider


def default_chat_callable(
    messages: list[dict[str, Any]],
    max_tokens: int = 800,
    *,
    provider_resolver: Callable[[], Any] = resolve_provider,
) -> str:
    """Production chat callable — delegates to the configured provider plugin.

    Construction route: ``kairix.config.yaml → provider_resolver() →
    ProviderChatBackend.chat``. Never raises; returns ``""`` on plugin
    error (honoured by ``ProviderChatBackend``).

    Tests pass ``provider_resolver=lambda: fake_provider`` to inject a
    Fake* through the public callable seam.
    """
    from kairix.transport.embed_service import ProviderChatBackend

    backend = ProviderChatBackend(provider_resolver())
    return backend.chat(messages, max_tokens=max_tokens)


def default_embed_callable(
    text: str,
    *,
    provider_resolver: Callable[[], Any] = resolve_provider,
) -> list[float]:
    """Production embed callable — delegates to the configured provider plugin.

    Routes through :class:`kairix.transport.embed_service.ProviderEmbeddingService`
    which owns cache + coalescer wiring. Never raises; returns ``[]`` on
    plugin error.

    Tests pass ``provider_resolver=lambda: fake_provider`` to inject a
    Fake* through the public callable seam.
    """
    from kairix.transport.embed_service import ProviderEmbeddingService

    svc = ProviderEmbeddingService(provider_resolver())
    return svc.embed(text)


@dataclass
class LLMBackendDeps:
    """Injectable dependencies for ``AzureOpenAIBackend``.

    Each field defaults to a provider-plugin-backed callable; tests construct
    ``LLMBackendDeps(chat=fake_chat, embed=fake_embed)`` rather than passing
    ``chat_fn=`` / ``embed_fn=`` kwargs to the backend's constructor.
    """

    chat: Callable[..., str] = field(default_factory=lambda: default_chat_callable)
    embed: Callable[[str], list[float]] = field(default_factory=lambda: default_embed_callable)


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
