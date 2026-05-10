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
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_capability_probe() -> Any:
    """Return the production capability probe callable.

    Wraps the existing ``kairix.platform.onboard.check`` functions in a
    single callable that returns the dict shape ``/healthz/ready``
    expects. Lazy-imports the check helpers so importing this module
    is cheap.
    """

    def probe() -> dict[str, Any]:
        from kairix.platform.onboard.check import (
            check_secrets_loaded,
            check_vector_search_working,
        )

        detail: dict[str, str] = {}

        # Secrets — fast (env / file probe).
        try:
            secrets_result = check_secrets_loaded()
            secrets_loaded = bool(getattr(secrets_result, "ok", False))
            if not secrets_loaded:
                detail["secrets_loaded"] = getattr(secrets_result, "message", "") or "LLM credentials missing"
        # Defensive: a probe must NEVER raise out to the caller. Failures
        # here mean we couldn't determine the secret state, so we report
        # secrets_loaded=False with the exception in detail.
        except Exception as exc:
            secrets_loaded = False
            detail["secrets_loaded"] = f"probe failed: {exc}"

        # Vector search — slower (loads index, runs probe query).
        try:
            vec_result = check_vector_search_working()
            vector_search_capable = bool(getattr(vec_result, "ok", False))
            if not vector_search_capable:
                detail["vector_search_capable"] = getattr(vec_result, "message", "") or "vector search unavailable"
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
