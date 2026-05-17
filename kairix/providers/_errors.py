"""Canonical error hierarchy for the kairix provider plugin layer.

Every provider plugin maps its upstream SDK / HTTP error types to one of
these canonical exceptions. The transport layer
(``kairix/transport/retry/``, ``kairix/transport/timeout/``) reasons
about provider failures through these typed errors rather than through
per-SDK exception classes — so "should I retry this?" is a single
``isinstance(err, RateLimited)`` check, not a per-provider switch.

The vocabulary was pinned by SK-4 in the provider-plugin-architecture
ADR (``docs/architecture/provider-plugin-architecture.md``). Renaming
any class breaks third-party plugin distributions that catch the exact
symbol, so the names are part of the public contract — the
``# noqa: N818`` directives below carry that rationale (ruff's N818
prefers an ``Error`` suffix on exception classes, which would force a
breaking rename of every published plugin and every operator support
runbook that pins these names).

Class summary:

- :class:`ProviderError` — common base for every provider failure.
- :class:`RateLimited` — upstream returned 429 / Retry-After.
- :class:`AuthError` — upstream returned 401 / 403.
- :class:`UpstreamError` — generic upstream 5xx with ``status_code``.
- :class:`ClientError` — non-retryable 4xx response (bad creds / model /
  endpoint URL). Distinct from :class:`AuthError` for cases the
  transport retry policy needs to short-circuit on.
- :class:`ProviderUnreachable` — connection refused / DNS failure.
- :class:`EmbedNotSupported` — provider has no embed surface
  (e.g. ``anthropic``); names the provider in the message.
- :class:`RetryExhausted` — transport's retry budget gave up; carries
  the count of attempts and the last underlying cause.
- :class:`TimeoutExceeded` — transport's timeout budget elapsed;
  carries the budget in milliseconds.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Common base for every kairix provider-plugin failure.

    Transport-layer code that needs to react to any provider failure
    (logging, metrics, fallback dispatch) catches this base; specific
    handlers (e.g. retry-on-429) catch the typed subclass instead.
    """


class RateLimited(ProviderError):  # noqa: N818 — ADR-pinned canonical name; see docs/architecture/provider-plugin-architecture.md
    """Upstream signalled rate-limit (HTTP 429 or equivalent).

    Carries the optional ``retry_after_s`` hint from the upstream
    response's ``Retry-After`` header so the transport retry policy can
    sleep the indicated duration instead of falling back to fixed
    backoff. ``None`` means the upstream did not provide a hint.
    """

    def __init__(self, message: str = "", *, retry_after_s: float | None = None) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(message)


class AuthError(ProviderError):
    """Upstream rejected the credential (HTTP 401 / 403).

    Surfaced to operators verbatim by ``kairix probe-config`` — the
    typical resolution is rotating the secret in the operator's secrets
    manager, not retrying.
    """


class ClientError(ProviderError):
    """Non-retryable 4xx response from the upstream (bad model id / URL / payload).

    The retry policy distinguishes transient (5xx, connection-reset)
    from client (4xx) failures: transient errors retry up to
    ``max_attempts``, client errors short-circuit to the caller on
    attempt 1. This is the typed error the caller sees for the latter,
    so operator triage can distinguish "bad credentials / bad model"
    from "endpoint overloaded". Distinct from :class:`AuthError` which
    specifically signals credential rejection (401/403).
    """

    def __init__(self, status: int, message: str = "") -> None:
        self.status = status
        suffix = f": {message}" if message else ""
        super().__init__(
            f"Provider returned client error status {status}{suffix}. "
            f"fix: check credentials / model id / endpoint URL — 4xx responses are "
            f"not retried because they indicate a caller-side problem the policy can't recover from."
        )


class UpstreamError(ProviderError):
    """Generic upstream server failure (HTTP 5xx).

    Carries the ``status_code`` so callers / dashboards can distinguish
    a 500 (transient) from a 503 (overload) from a 504 (gateway
    timeout). The retry policy treats all 5xx as retryable; specific
    consumers may downgrade on a per-status basis.
    """

    def __init__(self, message: str = "", *, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class ProviderUnreachable(ProviderError):  # noqa: N818 — ADR-pinned canonical name; see docs/architecture/provider-plugin-architecture.md
    """The configured endpoint did not accept a TCP / TLS connection.

    Connection refused, DNS resolution failed, TLS handshake aborted —
    anything that means we never got an HTTP response back. The retry
    policy treats this as retryable; ``probe-config`` surfaces it to
    operators as a config / network issue (wrong endpoint URL, firewall
    rule, no route, expired cert) rather than a credential issue.
    """


class EmbedNotSupported(ProviderError):  # noqa: N818 — ADR-pinned canonical name; see docs/architecture/provider-plugin-architecture.md
    """The configured provider does not offer an embeddings surface.

    Raised by ``Provider.embed_batch`` on providers like ``anthropic``
    that ship chat-only. Carries the ``provider_name`` so the operator
    sees which configured plugin is responsible and can switch to a
    different one for embed workloads (typical pattern: ``anthropic``
    for chat, ``openai`` for embed).
    """

    def __init__(self, message: str = "", *, provider_name: str) -> None:
        self.provider_name = provider_name
        if not message:
            message = (
                f"Provider {provider_name!r} does not support embeddings. "
                f"fix: configure a different provider for embed workloads "
                f"(e.g. KAIRIX_EMBED_PROVIDER=openai while keeping "
                f"KAIRIX_LLM_PROVIDER={provider_name!r} for chat)."
            )
        super().__init__(message)


class RetryExhausted(ProviderError):  # noqa: N818 — ADR-pinned canonical name; see docs/architecture/provider-plugin-architecture.md
    """The transport retry budget gave up after repeated upstream failures.

    Raised by ``kairix/transport/retry/`` once it has retried the
    underlying provider call ``attempts`` times and the last attempt
    still failed. ``last_cause`` wraps the underlying transient
    exception so operators can distinguish "retry policy gave up" from
    "caller threw an unrecoverable error on attempt 1".
    """

    def __init__(self, attempts: int, last_cause: BaseException | None = None) -> None:
        self.attempts = attempts
        self.last_cause = last_cause
        cause_repr = type(last_cause).__name__ if last_cause is not None else "no cause"
        super().__init__(
            f"Retry exhausted after {attempts} attempts (last cause: {cause_repr}). "
            f"fix: investigate the underlying transient failure, then either raise the "
            f"policy's max_attempts or escalate to a different provider endpoint."
        )


class TimeoutExceeded(ProviderError):  # noqa: N818 — ADR-pinned canonical name; see docs/architecture/provider-plugin-architecture.md
    """The transport timeout budget elapsed before the upstream replied.

    Raised by ``kairix/transport/timeout/``; ``budget_ms`` is the
    configured timeout in milliseconds so the surfaced error tells
    operators the budget they hit (and which knob to tune). Distinct
    from :class:`ProviderUnreachable` — the connection succeeded but
    the response took too long; the request may well have hit the
    upstream.
    """

    def __init__(self, budget_ms: int) -> None:
        self.budget_ms = budget_ms
        super().__init__(
            f"Provider call exceeded the configured {budget_ms} millisecond budget. "
            f"fix: investigate provider latency (network / endpoint distance / cold start), "
            f"or raise the budget for legitimately slow operations via the timeout policy."
        )


__all__ = [
    "AuthError",
    "ClientError",
    "EmbedNotSupported",
    "ProviderError",
    "ProviderUnreachable",
    "RateLimited",
    "RetryExhausted",
    "TimeoutExceeded",
    "UpstreamError",
]
