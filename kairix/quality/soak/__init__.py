"""Soak testing — repeat a workload and assert it holds together across iterations.

Catches the "unit-fine, scale-fragile" regression class: code that's correct
when invoked once but degrades on the Nth iteration (memory leak, log-spam,
per-call factory rebuild, fd leak, stateful contamination).

The #275 alpha-validation regression — `_resolve_legacy_collection_name`
firing N_agents x N_cases warning lines because `build_search_pipeline` was
rebuilt per case — would have been caught at PR time by a soak run that
asserted `stderr_bytes < N * max_per_case` across repeated benchmark runs.

Module API:
    from kairix.quality.soak import run_soak, SoakResult
    result = run_soak(suite="reflib", repeat=3)
    if not result.passed:
        for f in result.failures:
            print(f.reason)

Bindings:
    CLI:  kairix soak run --suite reflib --repeat 3
    MCP:  tool_soak_run (stub — returns OperatorOnlyCapability envelope)
"""

from kairix.quality.soak.runner import SoakIteration, SoakResult, run_soak

__all__ = ["SoakIteration", "SoakResult", "run_soak"]
