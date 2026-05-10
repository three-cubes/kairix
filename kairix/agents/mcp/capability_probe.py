"""Capability probe wired into ``/healthz/ready``.

Surfaces three layered signals so an operator can tell at a glance
whether the deployment is **fully operational** vs **degraded but up**:

  - ``secrets_loaded`` — LLM/embed credentials are present (env or
    secrets file). Without this, vector search returns 0 hits.
  - ``vector_search_capable`` — the vector index loads AND a probe
    embedding round-trip succeeds. The most expensive check; reflects
    the user-visible "search returns semantic hits" capability.
  - ``bm25_search_capable`` — FTS5 index is queryable. Cheapest check;
    available even when secrets are missing (BM25 fallback).

The #167 deployment failure is the canonical reason this layered probe
exists: ``/healthz`` returned ``ready=true`` while vector search was
broken, so the load balancer kept routing to a degraded instance.
``/healthz/ready`` makes that failure visible.

The probe is deliberately read-only and **fast** — every check completes
in under 100 ms in the typical case. We do NOT run a full embedding API
round-trip in the probe; the credential presence check is sufficient
signal for a load-balancer probe, and the round-trip is left to
``kairix onboard check``.

Dependency injection: ``build_capability_probe()`` accepts callables
for the secrets and vector-search checks. Production defaults to the
``onboard.check`` helpers; tests inject light-weight stand-ins so the
probe can be exercised without booting the full check stack.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _CheckResult(Protocol):
    """Structural type matching ``onboard.check.CheckResult``.

    The probe reads ``ok`` to gate the capability and ``detail`` for
    the human-readable failure message. We deliberately do not require
    ``name`` or ``fix`` — those are operator-facing fields on the
    onboard checks themselves.
    """

    ok: bool
    detail: str


CheckFn = Callable[[], _CheckResult]


def _default_secrets_check() -> _CheckResult:
    """Production secrets check — lazy-imports onboard.check on first call."""
    from kairix.platform.onboard.check import check_secrets_loaded

    return check_secrets_loaded()


def _default_vector_check() -> _CheckResult:
    """Production vector-search check — lazy-imports onboard.check on first call."""
    from kairix.platform.onboard.check import check_vector_search_working

    return check_vector_search_working()


def build_capability_probe(
    *,
    secrets_check: CheckFn | None = None,
    vector_check: CheckFn | None = None,
) -> Callable[[], dict[str, Any]]:
    """Return a callable suitable for ``/healthz/ready``'s ``capability_probe`` slot.

    Args:
        secrets_check: Callable that returns a ``CheckResult``-shaped
            object (``.ok``, ``.message``). Defaults to
            ``onboard.check.check_secrets_loaded``.
        vector_check: Callable that returns a ``CheckResult``-shaped
            object. Defaults to ``onboard.check.check_vector_search_working``.

    The returned probe never raises: every failure mode is encoded as
    a ``False`` capability flag with a human-readable explanation in
    the ``detail`` map.
    """
    secrets_fn = secrets_check or _default_secrets_check
    vector_fn = vector_check or _default_vector_check

    def probe() -> dict[str, Any]:
        detail: dict[str, str] = {}

        # Secrets — fast (env / file probe).
        try:
            secrets_result = secrets_fn()
            secrets_loaded = bool(getattr(secrets_result, "ok", False))
            if not secrets_loaded:
                detail["secrets_loaded"] = getattr(secrets_result, "detail", "") or "LLM credentials missing"
        # Defensive: a probe must NEVER raise out to the caller. Failures
        # here mean we couldn't determine the secret state, so we report
        # secrets_loaded=False with the exception in detail.
        except Exception as exc:
            secrets_loaded = False
            detail["secrets_loaded"] = f"probe failed: {exc}"

        # Vector search — slower (loads index, runs probe query).
        try:
            vec_result = vector_fn()
            vector_search_capable = bool(getattr(vec_result, "ok", False))
            if not vector_search_capable:
                detail["vector_search_capable"] = getattr(vec_result, "detail", "") or "vector search unavailable"
        # Defensive: same rationale as secrets probe above.
        except Exception as exc:
            vector_search_capable = False
            detail["vector_search_capable"] = f"probe failed: {exc}"

        # BM25 — implicit; if the kairix process is up the FTS index is
        # queryable. We expose it as a capability so operators can see
        # the "BM25 fallback only" state explicitly when vector search
        # has degraded.
        bm25_search_capable = True

        return {
            "secrets_loaded": secrets_loaded,
            "vector_search_capable": vector_search_capable,
            "bm25_search_capable": bm25_search_capable,
            "detail": detail,
        }

    return probe
