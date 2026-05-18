"""Warm-up — pre-load kairix caches and pay factory-init costs before agent traffic.

Module API:
    from kairix.platform.warm import run_warm, WarmResult
    result = run_warm()
    if not result.ok:
        for failure in result.failures:
            print(failure.step, failure.detail)

Bindings:
    CLI:  kairix warm
    MCP:  tool_warm (real binding — idempotent, fast once warm)
"""

from kairix.platform.warm.runner import WARMUP_QUERY, WarmFailure, WarmResult, WarmStep, run_warm

__all__ = ["WARMUP_QUERY", "WarmFailure", "WarmResult", "WarmStep", "run_warm"]
