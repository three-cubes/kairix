"""Provider-backed ``ChatBackend`` adapter for the eval module.

The eval module's ``LLMJudge`` / ``QueryGenerator`` / ``LLMJudgeScorer`` /
``GoldBuilder`` callers consume the :class:`kairix.core.protocols.ChatBackend`
protocol — the ``complete(prompt, *, api_key, endpoint, deployment, ...)``
shape.

This module adapts a :class:`kairix.providers.Provider` (the plugin
Protocol) to that surface so the eval module routes through the
provider plugin layer rather than any concrete vendor adapter. The
transport layer's
:class:`kairix.transport.embed_service.ProviderChatBackend` exposes a
different ``chat(messages, max_tokens)`` shape that suits the
LLMBackend consumers (briefing, summaries, query-planner); the eval
surface needs the ``complete(prompt, ...)`` shape, so it gets its own
adapter here under ``kairix/quality/eval/`` — the boundary that owns
the ``ChatBackend`` protocol consumers.

Construction
------------

Production callers build the adapter via::

    from kairix.providers import get_provider
    from kairix.paths import provider_name
    from kairix.quality.eval.chat_backend import ProviderEvalChatBackend

    backend = ProviderEvalChatBackend.from_config()

Tests construct ``FakeChatBackend(...)`` from ``tests/fakes.py`` directly
— the adapter is only needed when wiring a real provider plugin, which
production callers do at the construction point of ``LLMJudge`` /
``LLMJudgeScorer`` / ``GoldBuilder``.

F26 note: this module lives under ``kairix/quality/eval/`` (not
``kairix/core/``) so it is free to import from ``kairix/providers/``
and ``kairix/paths``. The boundary protocol it implements
(``ChatBackend``) is in ``kairix/core/protocols.py``, but consuming a
protocol from core does not violate F26 — only ``kairix/core/**``
modules are forbidden from reaching into ``providers/`` / ``transport/``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from kairix.providers import Provider


class ProviderEvalChatBackend:
    """Adapt a :class:`kairix.providers.Provider` to the eval ``ChatBackend`` surface.

    Translates the ``ChatBackend.complete(prompt, *, api_key, endpoint,
    deployment, system=None, temperature=0.0, timeout_s=30.0) -> str``
    shape into the ``Provider.chat(messages, max_tokens) -> str`` shape
    that the plugin layer speaks.

    Credential plumbing semantic:

      - ``api_key`` / ``endpoint`` / ``deployment`` are accepted for
        protocol conformance but ignored. The provider plugin resolves
        its own credentials from the configured secret source (Azure
        Key Vault for ``azure_foundry``, env / file for others), so the
        eval module no longer plumbs them through.
      - ``temperature`` / ``timeout_s`` are accepted for protocol
        conformance; the provider applies its own per-plugin tuning.

    The provider's own ``chat`` is never-raises — returns ``""`` on
    plugin error. This adapter preserves that contract: the eval
    callers see the empty string and treat it as "judge returned
    nothing", which already maps to all-zero grades in ``LLMJudge``.
    """

    _DEFAULT_MAX_TOKENS: int = 800

    def __init__(self, provider: Provider) -> None:
        self._provider = provider

    @classmethod
    def from_config(cls) -> ProviderEvalChatBackend:
        """Build the production adapter via ``provider_name()`` + ``get_provider``.

        Reads the configured plugin name from ``kairix.config.yaml``
        (via :func:`kairix.paths.provider_name`) and resolves it to a
        ``Provider`` via :func:`kairix.providers.get_provider`.

        Raises ``ValueError`` when no provider is configured — same
        failure mode operators see when other provider-backed surfaces
        (embedding service, briefing synthesiser) hit the same missing
        config.
        """
        from kairix.paths import provider_name
        from kairix.providers import get_provider

        name = provider_name()
        if name is None:
            raise ValueError(
                "kairix.config.yaml is missing the required 'provider:' field — "
                "fix: set 'provider: <name>' in your config; "
                "next: run `kairix probe-config` to verify the plugin loads."
            )
        return cls(get_provider(name))

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        **_kwargs: object,
    ) -> str:
        """Run a chat completion through the configured provider plugin.

        Builds the ``messages`` list (system + user) and delegates to
        ``Provider.chat``. The ``ChatBackend`` protocol surface accepts
        ``api_key`` / ``endpoint`` / ``deployment`` / ``temperature`` /
        ``timeout_s`` kwargs for vendor-specific tuning; this adapter
        absorbs them into ``**_kwargs`` because the provider plugin
        resolves credentials and tuning internally (per the plugin
        architecture in
        ``docs/architecture/provider-plugin-architecture.md``).

        Structural protocol matching (``runtime_checkable``) only cares
        that ``complete`` is callable with the protocol's keyword
        arguments; ``**kwargs`` satisfies that without forcing F19-
        flagged unused-named-parameter placeholders.
        """
        del _kwargs
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            return self._provider.chat(messages, max_tokens=self._DEFAULT_MAX_TOKENS)
        except Exception as exc:
            logger.warning("ProviderEvalChatBackend.complete: provider raised — %s", exc)
            return ""


__all__ = ["ProviderEvalChatBackend"]
