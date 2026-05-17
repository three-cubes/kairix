"""F29: performance-measurement code may only live under ``kairix/quality/probe/``.

The three-layer provider-plugin split (see
``docs/architecture/provider-plugin-architecture.md`` - "Performance")
centralises every layer's latency / throughput instrumentation in one
location: ``kairix/quality/probe/``. The probe reads each layer
through one uniform timing hook and is the single surface both the
PVT release gate and the end-user ``kairix probe-config`` health
check invoke. Letting ``transport/`` or ``providers/`` grow their own
ad-hoc benchmark scripts re-creates the per-provider conditionals the
ADR exists to remove.

F29 detects any ``.py`` file whose name matches a benchmark / perf /
microbench / latency naming pattern and lives outside the allowed
roots.

Allowed roots (perf scripts welcome here):
  - ``kairix/quality/probe/**`` — the canonical home.
  - ``tests/**`` — test files may assert on latency or coalesce-ratio
    bounds (those assertions are the gate, not new measurement
    plumbing).
  - ``scripts/probe*`` — operational scripts that drive the probe.

Rejected paths (where a perf-named file is a regression):
  - ``kairix/transport/**`` (instrumented via the probe's hook)
  - ``kairix/providers/**`` (instrumented via the probe's hook)
  - any other location under ``kairix/`` outside ``kairix/quality/probe/``

File-name patterns considered perf-measurement:
  - ``bench*.py``, ``microbench*.py``
  - ``*_bench.py``, ``*_microbench.py``
  - ``*_latency.py``, ``*_latency_*.py``
  - ``*_perf.py``, ``*_perf_*.py``

If ``kairix/`` does not exist (fresh checkout) the check still walks
the rest of the tree; if no perf-named files exist anywhere outside
the allow-list, the check passes trivially.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate

# Regex that matches a perf-measurement-shaped filename. Anchored at
# both ends — we only flag files whose entire basename matches.
_PERF_NAME_RE = re.compile(
    r"""
    ^(
        bench[a-z0-9_]*           # bench.py, benchmarks.py, bench_provider.py
        | microbench[a-z0-9_]*    # microbench.py, microbench_foo.py
        | [a-z0-9_]+_bench        # http_bench.py, transport_bench.py
        | [a-z0-9_]+_microbench   # http_microbench.py
        | [a-z0-9_]+_latency      # embed_latency.py
        | [a-z0-9_]+_latency_[a-z0-9_]+
        | [a-z0-9_]+_perf         # http_perf.py
        | [a-z0-9_]+_perf_[a-z0-9_]+
    )\.py$
    """,
    re.VERBOSE,
)

# Directories whose contents are explicitly allowed to contain perf
# measurement code. A path is allowed if it lives under ANY of these
# prefixes (relative to repo root).
_ALLOWED_PREFIXES: tuple[Path, ...] = (
    Path("kairix") / "quality" / "probe",
    Path("tests"),
)

# Prefix for operational probe-driver scripts at the repo root.
_ALLOWED_SCRIPTS_PREFIX = Path("scripts")
_ALLOWED_SCRIPTS_NAME_RE = re.compile(r"^probe[a-z0-9_-]*\.(py|sh)$")

REMEDIATION = """Refactor to move the performance-measurement code into
kairix/quality/probe/ — that's the single perf surface for the whole
project, exposed through the probe CLI and the kairix probe-config
end-user command.

fix: relocate the bench/microbench/latency script under
kairix/quality/probe/<subarea>/, expose its entry point through the
probe CLI (kairix probe ... or a new subcommand), and consume the
timings hook from kairix/transport/telemetry/ (or the per-layer
equivalent) rather than reinventing a measurement harness. If the
measurement is a test assertion (e.g. "p99 < 200ms under fake clock"),
move it under tests/ — F29's allow-list covers that.
next: re-run python3 scripts/checks/check_perf_singleton.py to
confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(probe): consolidate <metric> measurement"

Pass example:
  kairix/quality/probe/embed_latency.py       # canonical home — allowed
  tests/integration/test_embed_perf_floor.py  # latency assertion in a test — allowed
  scripts/probe-config-runner.py              # operational driver — allowed

Forbidden example:
  kairix/transport/pool/bench_pool.py         # F29 — perf code in transport
  kairix/providers/openai/openai_perf.py      # F29 — perf code in a plugin
  kairix/core/search/bm25_latency.py          # F29 — perf code in domain

Why: see docs/architecture/provider-plugin-architecture.md -
"Performance". The probe is the single perf surface so the PVT and
end-user health-check share one implementation and the report schema
stays stable. Parallel benchmark harnesses scattered across
transport/ and providers/ create the per-provider conditional jungle
the ADR exists to remove."""


def _is_perf_named(name: str) -> bool:
    """True if ``name`` is a basename that matches a perf-measurement
    naming pattern (bench/microbench/latency/perf)."""
    return _PERF_NAME_RE.match(name) is not None


def _is_allowed(rel_path: Path) -> bool:
    """True if a perf-named file at ``rel_path`` (relative to repo
    root) is allowed under F29's exception roots.
    """
    parts = rel_path.parts
    for prefix in _ALLOWED_PREFIXES:
        prefix_parts = prefix.parts
        if len(parts) >= len(prefix_parts) and tuple(parts[: len(prefix_parts)]) == prefix_parts:
            return True
    # scripts/probe*.{py,sh} is also allowed — the operational
    # driver scripts that invoke the probe.
    if len(parts) >= 2 and parts[0] == _ALLOWED_SCRIPTS_PREFIX.name and _ALLOWED_SCRIPTS_NAME_RE.match(parts[-1]):
        return True
    return False


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every .py file under ``repo_root/kairix/`` and return
    repo-relative paths of perf-named files that live outside the
    allowed roots.

    Note: the scan is deliberately scoped to ``kairix/`` (not the
    entire repo tree). Perf-named files under top-level ``tools/`` or
    ``scratch/`` are explicitly out of scope — F29 protects the
    production package boundary. Files under ``tests/`` are excluded
    by ``_is_allowed``.
    """
    kairix_dir = repo_root / "kairix"
    if not kairix_dir.exists():
        return set()

    violations: set[Path] = set()
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if not _is_perf_named(path.name):
            continue
        try:
            rel = path.resolve().relative_to(repo_root)
        except ValueError:
            continue
        if _is_allowed(rel):
            continue
        violations.add(rel)
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f29", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
