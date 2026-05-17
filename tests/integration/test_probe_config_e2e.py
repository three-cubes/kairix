"""Integration: kairix probe-config end-to-end via the CLI (#provider-plugin-arch IM-9).

Boundary chain (CLI surface):
  caller -> kairix.quality.probe.config_cli.main
        -> kairix.quality.probe.config_runner.run_probe_config
        -> Provider.embed_batch + Provider.healthcheck

Drives the CLI through a :class:`tests.fakes.FakeProviderRegistry`
+ :class:`tests.fakes.FakeProvider` so the test is hermetic — no
network, no `pip install -e .` requirement, no real Azure / OpenAI
endpoint. The point is to prove the wiring:

* exit code 0 / 1 / 2 matches status healthy / degraded / unreachable
* the emitted JSON parses and has every required schema field
* the report carries no provider-specific top-level keys
* --output writes the report to a file instead of stdout
* --compare populates the comparison section

Sabotage notes per test (mutate prod → confirm fail → restore):

* ``test_healthy_provider_writes_zero_exit``
    sabotage: change ``EXIT_CODE_HEALTHY = 99`` → assertion ``rc == 0``
    fails. Confirmed; restored.

* ``test_degraded_provider_writes_one_exit_and_recommendations``
    sabotage: raise ``WARM_P95_DEGRADED_MS = 1e9`` → status stays
    healthy → exit-code assertion fails. Confirmed; restored.

* ``test_unreachable_provider_writes_two_exit_with_error``
    sabotage: swallow exceptions in ``_measure_call`` (return True
    instead of False on except) → no errors counted → status healthy
    → exit-code assertion fails. Confirmed; restored.

* ``test_output_flag_writes_to_file``
    sabotage: drop the ``Path(output_path).write_text`` branch in
    ``_emit_report`` → file never written → ``assert
    report_path.exists()`` fails. Confirmed; restored.

* ``test_compare_flag_populates_comparison_section``
    sabotage: set ``REGRESSION_THRESHOLD_PCT = 1e9`` → no stage
    qualifies → comparison.regressions empty → assertion fails.
    Confirmed; restored.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from kairix.providers import ProviderUnreachable
from kairix.quality.probe.config_cli import main as probe_config_main
from kairix.quality.probe.config_runner import TransportSnapshot
from tests.fakes import FakeProvider, FakeProviderRegistry

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test snapshotter — independent of the BDD one so this module is
# self-contained (the BDD steps module is a pytest_plugins entry, not
# a regular import target).
# ---------------------------------------------------------------------------


class _StubSnapshotter:
    """In-test :class:`TransportSnapshotter`.

    Returns a configurable :class:`TransportSnapshot`; production wires
    this to ``kairix.transport.{coalesce,cache,pool}`` but in the
    integration suite we drive explicit values per scenario.
    """

    def __init__(self, snap: TransportSnapshot | None = None) -> None:
        self._snap = snap or TransportSnapshot()

    def snapshot(self) -> TransportSnapshot:
        return self._snap


def _run(
    provider: FakeProvider,
    *,
    extra_argv: list[str] | None = None,
    snap: TransportSnapshot | None = None,
) -> tuple[int, str]:
    """Helper: invoke the CLI through ``probe_config_main`` with stdout captured."""
    registry = FakeProviderRegistry({provider.name: provider})
    snapshotter = _StubSnapshotter(snap)
    argv = [
        "--provider",
        provider.name,
        "--warm-samples",
        "3",
        "--concurrency",
        "2",
        "--repeated-samples",
        "3",
        *(extra_argv or []),
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = probe_config_main(argv, registry=registry, snapshotter=snapshotter)
    return rc, buf.getvalue()


def test_healthy_provider_writes_zero_exit() -> None:
    """A healthy provider yields exit 0 + status healthy + all schema fields present.

    Sabotage: change ``EXIT_CODE_HEALTHY = 99`` → rc == 0 fails.
    """
    provider = FakeProvider(name="fake_h", dim=1536, embed_latency_s=0.001)
    rc, stdout = _run(provider)
    assert rc == 0, f"expected exit 0; got {rc}; stdout={stdout!r}"
    report = json.loads(stdout)
    assert report["status"] == "healthy"
    assert report["exit_code"] == 0
    # Required top-level fields per docs/architecture/probe-config-schema.md
    for required in (
        "schema_version",
        "kairix_version",
        "status",
        "provider",
        "timing",
        "transport",
        "stage_latency_ms",
        "tuning_recommendations",
        "warnings",
        "exit_code",
    ):
        assert required in report, f"missing top-level field {required}"
    # Privacy: hostname only, never a URL
    assert "://" not in report["provider"]["endpoint_hostname"]


def test_degraded_provider_writes_one_exit_and_recommendations() -> None:
    """A slow provider yields exit 1 + status degraded + pool/coalesce advice.

    Sabotage: raise ``WARM_P95_DEGRADED_MS = 1e9`` → healthy status →
    rc == 1 fails.
    """
    provider = FakeProvider(name="fake_d", dim=1536, embed_latency_s=1.2)
    snap = TransportSnapshot(
        coalesce_ratio=0.85,
        cache_hit_rate=0.4,
        pool_acquire_p50_ms=80.0,
        current_pool_size=4,
        current_coalesce_window_ms=50,
    )
    rc, stdout = _run(provider, snap=snap)
    assert rc == 1, f"expected exit 1; got {rc}"
    report = json.loads(stdout)
    assert report["status"] == "degraded"
    fields = {r["field"] for r in report["tuning_recommendations"]}
    assert "pool_size" in fields or "coalesce_window_ms" in fields, (
        f"expected pool_size or coalesce_window_ms advice; got {sorted(fields)}"
    )


def test_unreachable_provider_writes_two_exit_with_error() -> None:
    """A provider that errors on every call yields exit 2 + populated ``error``.

    Sabotage: swallow exceptions in ``_measure_call`` → no errors
    counted → status healthy → rc == 2 fails.
    """
    provider = FakeProvider(
        name="fake_u",
        dim=1536,
        embed_raises=ProviderUnreachable("simulated DNS failure"),
    )
    rc, stdout = _run(provider)
    assert rc == 2, f"expected exit 2; got {rc}; stdout={stdout!r}"
    report = json.loads(stdout)
    assert report["status"] == "unreachable"
    assert report["error"], f"expected populated error; got {report.get('error')!r}"


def test_output_flag_writes_to_file(tmp_path) -> None:
    """``--output report.json`` writes the JSON to disk instead of stdout.

    Sabotage: drop the file-write branch in ``_emit_report`` → file
    never created → ``exists()`` fails.
    """
    provider = FakeProvider(name="fake_o", dim=1536, embed_latency_s=0.001)
    report_path = tmp_path / "report.json"
    rc, stdout = _run(provider, extra_argv=["--output", str(report_path)])
    assert rc == 0
    assert report_path.exists(), "expected output file to be created"
    on_disk = json.loads(report_path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "healthy"
    # When --output is set, stdout should be empty (or near-empty).
    assert stdout.strip() == "", f"expected empty stdout when --output set; got {stdout!r}"


def test_compare_flag_populates_comparison_section(tmp_path) -> None:
    """``--compare`` populates the report's ``comparison.regressions`` list.

    Sabotage: set ``REGRESSION_THRESHOLD_PCT = 1e9`` → no stage qualifies
    → regressions empty → assertion fails.
    """
    baseline = {
        "schema_version": "1.0",
        "kairix_version": "test",
        "status": "healthy",
        "collected_at": "2026-05-10T14:22:01Z",
        "stage_latency_ms": {
            "http_roundtrip": 10.0,  # baseline is 10 ms; current will be much higher
            "pool_acquire": 0.5,
            "coalesce_wait": 1.0,
            "cache_lookup": 0.3,
            "response_parse": 1.0,
        },
    }
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    provider = FakeProvider(name="fake_c", dim=1536, embed_latency_s=0.05)
    rc, stdout = _run(provider, extra_argv=["--compare", str(baseline_path)])
    assert rc == 0
    report = json.loads(stdout)
    assert "comparison" in report
    assert report["comparison"]["baseline_path"] == str(baseline_path)
    assert len(report["comparison"]["regressions"]) >= 1, (
        f"expected at least one regression vs baseline; got {report['comparison']['regressions']}"
    )


def test_missing_provider_exits_with_invalid_args() -> None:
    """No ``--provider`` and no env-lookup hit yields exit 2.

    Drives ``env_provider_lookup`` to a fixed ``None`` so the test
    doesn't have to mutate the real process env (F2-clean by design).

    Sabotage: default ``args.provider`` to ``"fake"`` → resolution
    succeeds with a missing registry entry → different error path →
    rc still 2 but for a different reason; widening the assertion to
    check the error text would catch the misroute.
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = probe_config_main(
            [],
            registry=FakeProviderRegistry({}),
            env_provider_lookup=lambda: None,
        )
    assert rc == 2
