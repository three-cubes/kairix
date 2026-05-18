"""Unit tests for F29 (``scripts/checks/check_perf_singleton.py``).

F29 forbids perf-measurement-named Python files (``bench*.py``,
``*_latency*.py``, ``*_perf*.py``, ``microbench*.py``) anywhere
under ``kairix/`` except ``kairix/quality/probe/**``. Tests and
``scripts/probe*`` operational drivers are allowed.

Each test has an inline sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_perf_singleton.py"


def _load_detector():
    """Load the F29 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f29_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f29_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_perf_file_under_probe_is_allowed(tmp_path: Path) -> None:
    """``kairix/quality/probe/embed_latency.py`` is the canonical home
    — never flagged.

    Sabotage-proof inline: relocate the file to ``kairix/transport/``;
    the detector fires.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "quality" / "probe" / "embed_latency.py"
    _write(target, "# measure embed latency\n")
    assert detector.collect_violations(tmp_path) == set()

    # Sabotage: same filename under transport/.
    target.unlink()
    sabotage = tmp_path / "kairix" / "transport" / "embed_latency.py"
    _write(sabotage, "# measure embed latency\n")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/transport/embed_latency.py") in violations


def test_bench_file_in_transport_is_flagged(tmp_path: Path) -> None:
    """A ``bench_pool.py`` under ``kairix/transport/pool/`` is rejected.

    Sabotage-proof inline: relocate under ``kairix/quality/probe/``;
    the flag clears.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "transport" / "pool" / "bench_pool.py"
    _write(target, "")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/transport/pool/bench_pool.py") in violations

    # Sabotage.
    target.unlink()
    _write(tmp_path / "kairix" / "quality" / "probe" / "bench_pool.py", "")
    assert detector.collect_violations(tmp_path) == set()


def test_perf_file_in_provider_is_flagged(tmp_path: Path) -> None:
    """A perf-named file under ``kairix/providers/<plugin>/`` is
    rejected — providers route measurement through the probe hook.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "providers" / "openai" / "openai_perf.py"
    _write(target, "")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/providers/openai/openai_perf.py") in violations


def test_latency_named_file_in_core_is_flagged(tmp_path: Path) -> None:
    """``*_latency.py`` under ``kairix/core/`` (domain layer) is
    rejected.
    """
    detector = _load_detector()
    target = tmp_path / "kairix" / "core" / "search" / "bm25_latency.py"
    _write(target, "")
    violations = detector.collect_violations(tmp_path)
    assert Path("kairix/core/search/bm25_latency.py") in violations


def test_non_perf_files_are_not_flagged(tmp_path: Path) -> None:
    """A file whose name doesn't match the perf pattern is left alone,
    no matter where it sits.
    """
    detector = _load_detector()
    _write(tmp_path / "kairix" / "transport" / "pool" / "client.py", "")
    _write(tmp_path / "kairix" / "providers" / "openai" / "embed.py", "")
    _write(tmp_path / "kairix" / "core" / "search" / "bm25.py", "")
    assert detector.collect_violations(tmp_path) == set()


def test_missing_kairix_directory_passes(tmp_path: Path) -> None:
    """Fresh checkout: no ``kairix/`` directory — gate green."""
    detector = _load_detector()
    assert detector.collect_violations(tmp_path) == set()


def test_perf_name_regex_matches_expected_shapes() -> None:
    """The naming regex picks up the documented perf-measurement
    file patterns: bench / microbench / *_bench / *_latency / *_perf.
    """
    detector = _load_detector()
    for good in (
        "bench.py",
        "benchmarks.py",
        "bench_pool.py",
        "microbench.py",
        "microbench_openai.py",
        "http_bench.py",
        "embed_latency.py",
        "embed_latency_p99.py",
        "http_perf.py",
        "http_perf_floor.py",
    ):
        assert detector._is_perf_named(good), good
    for bad in (
        "client.py",
        "embed.py",
        "telemetry.py",
        "probe.py",
        "config.py",
        "auth.py",
    ):
        assert not detector._is_perf_named(bad), bad


def test_real_repo_gate_is_green() -> None:
    """The real F29 detector run against the full repo emits no
    net-new violations vs ``.architecture/baseline/F29-files.txt``.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F29's REMEDIATION must satisfy F21."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
