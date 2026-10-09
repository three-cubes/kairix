"""Unit tests for ``scripts/cutover/diff_baseline.py``.

Each test constructs a synthetic pre/post baseline pair and asserts the
expected per-gate verdict + overall summary. The script is the
deterministic core of the cutover protocol's hard-gate check
(``docs/architecture/feature-flag-architecture.md`` §4.2 Step 5).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.cutover.diff_baseline import (
    _build_report,
    _check_gate_benchmark,
    _check_gate_latency,
    _check_gate_sample_journey,
    _check_gate_state,
    main,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — canonical baseline shapes used across tests
# ---------------------------------------------------------------------------


def _baseline(flag: str = "test_flag") -> dict[str, Any]:
    """A canonical baseline envelope — every surface populated + healthy."""
    return {
        "flag": flag,
        "captured_at": "2026-05-23T09:00:00Z",
        "version": "v2026.5.23",
        "state": {
            "per_collection": [
                {"collection": "vault", "doc_count": 1000, "total_bytes": 5_000_000},
                {"collection": "crm", "doc_count": 500, "total_bytes": 2_000_000},
            ],
            "content_hash_digest": "sha256:abc",
        },
        "eval": {
            "reflib": {"recall_at_10": 0.90},
            "locomo": {"recall": 0.40},
        },
        "latency": {"p50_ms": 40.0, "p95_ms": 120.0, "p99_ms": 300.0},
        "sample_journey": [
            {
                "query": f"q{i}",
                "top_paths": [f"q{i}-doc-{j}.md" for j in range(5)],
            }
            for i in range(5)
        ],
    }


# ---------------------------------------------------------------------------
# Identical pre/post -> ALL GATES PASS
# ---------------------------------------------------------------------------


def test_identical_baselines_all_gates_pass() -> None:
    """If pre == post, every gate passes; overall verdict is ALL GATES PASS."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["captured_at"] = "2026-05-24T09:00:00Z"
    report = _build_report(pre, post)
    assert {g["name"]: g["status"] for g in report["gates"]} == {
        "state": "pass",
        "eval": "pass",
        "latency": "pass",
        "sample_journey": "pass",
    }
    assert report["overall"] == "ALL GATES PASS"


# ---------------------------------------------------------------------------
# State gate — >2% delta fails
# ---------------------------------------------------------------------------


def test_state_gate_fails_when_doc_count_drifts_over_2pct() -> None:
    """A +5% drift in any collection's doc_count fails the state gate."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["state"]["per_collection"][0]["doc_count"] = 1050  # +5%
    result = _check_gate_state(pre["state"], post["state"])
    assert result.status == "fail"
    assert "vault" in result.detail


def test_state_gate_passes_at_exactly_2pct() -> None:
    """A +2% drift is on the boundary and passes (per ±2% tolerance)."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["state"]["per_collection"][0]["doc_count"] = 1020  # +2%
    post["state"]["per_collection"][0]["total_bytes"] = 5_100_000  # +2%
    result = _check_gate_state(pre["state"], post["state"])
    assert result.status == "pass"


def test_state_gate_fails_when_total_bytes_drifts_over_2pct() -> None:
    """A +10% drift in total_bytes alone (counts unchanged) fails the gate."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["state"]["per_collection"][0]["total_bytes"] = 5_500_000  # +10%
    result = _check_gate_state(pre["state"], post["state"])
    assert result.status == "fail"


def test_state_gate_skipped_when_surface_missing() -> None:
    """Missing state surface on either side -> skip, not fail."""
    pre = _baseline()
    result = _check_gate_state(pre["state"], None)
    assert result.status == "skip"


# ---------------------------------------------------------------------------
# Benchmark recall gate — reflib ±2pp, LoCoMo ±3pp
# ---------------------------------------------------------------------------


def test_recall_gate_fails_on_reflib_3pp_drop() -> None:
    """A 3pp drop in reflib recall_at_10 fails (tolerance is 2pp)."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["eval"]["reflib"]["recall_at_10"] = 0.87  # -3pp
    result = _check_gate_benchmark(pre["eval"], post["eval"])
    assert result.status == "fail"


def test_recall_gate_passes_on_reflib_1pp_drop() -> None:
    """A 1pp drop is within the 2pp reflib tolerance."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["eval"]["reflib"]["recall_at_10"] = 0.89
    result = _check_gate_benchmark(pre["eval"], post["eval"])
    assert result.status == "pass"


def test_recall_gate_passes_on_locomo_2pp_drop() -> None:
    """LoCoMo has a 3pp tolerance — a clean 2pp drop is well within band."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["eval"]["locomo"]["recall"] = 0.38  # -2pp, comfortably under 3pp
    result = _check_gate_benchmark(pre["eval"], post["eval"])
    assert result.status == "pass"


def test_recall_gate_fails_on_locomo_5pp_drop() -> None:
    """A 5pp LoCoMo drop is well outside the 3pp tolerance — fails."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["eval"]["locomo"]["recall"] = 0.35  # -5pp
    result = _check_gate_benchmark(pre["eval"], post["eval"])
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# Latency gate — P95 ±20%
# ---------------------------------------------------------------------------


def test_latency_gate_fails_when_p95_doubles() -> None:
    """A 2x P95 (+100%) blows past the ±20% tolerance."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["latency"]["p95_ms"] = 240.0  # +100%
    result = _check_gate_latency(pre["latency"], post["latency"])
    assert result.status == "fail"


def test_latency_gate_passes_at_15pct_increase() -> None:
    """+15% P95 is within the ±20% tolerance."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["latency"]["p95_ms"] = 138.0  # +15%
    result = _check_gate_latency(pre["latency"], post["latency"])
    assert result.status == "pass"


def test_latency_gate_skipped_when_p95_missing() -> None:
    """Missing p95_ms on one side -> skip."""
    pre = _baseline()
    bad_post = {"p50_ms": 40.0}  # missing p95/p99
    result = _check_gate_latency(pre["latency"], bad_post)
    assert result.status == "skip"


# ---------------------------------------------------------------------------
# Sample-journey gate — >=80% queries keep >=3/5 of top-5
# ---------------------------------------------------------------------------


def test_journey_gate_fails_when_50pct_queries_drift() -> None:
    """If half the queries lose 3+ of their top-5, parity falls below 80%."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    # Mangle q0, q1 — replace all 5 paths with fresh ones (kept = 0)
    for idx in (0, 1):
        post["sample_journey"][idx]["top_paths"] = [f"drift-{idx}-{j}.md" for j in range(5)]
    # q2, q3, q4 unchanged -> 3/5 pass = 60% parity < 80%
    result = _check_gate_sample_journey(pre["sample_journey"], post["sample_journey"])
    assert result.status == "fail"
    assert "60%" in result.detail


def test_journey_gate_passes_with_minor_reordering() -> None:
    """Reordering within the top-5 doesn't drop overlap below 3/5."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    # For each query, swap paths around but keep all 5 entries
    for entry in post["sample_journey"]:
        entry["top_paths"] = list(reversed(entry["top_paths"]))
    result = _check_gate_sample_journey(pre["sample_journey"], post["sample_journey"])
    assert result.status == "pass"


def test_journey_gate_fails_when_two_paths_swap_in_every_query() -> None:
    """Swap 3 of 5 paths in every query — kept becomes 2/5 everywhere -> 0% parity."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    for entry in post["sample_journey"]:
        entry["top_paths"] = [
            entry["top_paths"][0],
            entry["top_paths"][1],
            "new-a.md",
            "new-b.md",
            "new-c.md",
        ]
    result = _check_gate_sample_journey(pre["sample_journey"], post["sample_journey"])
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# --strict mode propagates failure as non-zero exit
# ---------------------------------------------------------------------------


def _write_baseline(path: Path, payload: dict[str, Any]) -> None:
    """Serialise a baseline payload to ``path`` as JSON."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_strict_mode_exits_nonzero_on_any_gate_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--strict + any failure -> exit 1 (so CI / deploy pipelines can wire it up)."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["latency"]["p95_ms"] = 500.0  # +316% — clearly fails latency gate
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)
    rc = main(["--pre", str(pre_path), "--post", str(post_path), "--strict"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "ROLLBACK RECOMMENDED" in out


def test_non_strict_mode_always_exits_zero(tmp_path: Path) -> None:
    """Default mode reports + exits 0 so the operator can read the table."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["latency"]["p95_ms"] = 500.0
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)
    rc = main(["--pre", str(pre_path), "--post", str(post_path)])
    assert rc == 0


def test_strict_mode_zero_on_full_pass(tmp_path: Path) -> None:
    """--strict + all-pass -> exit 0."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["captured_at"] = "2026-05-24T09:00:00Z"
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)
    rc = main(["--pre", str(pre_path), "--post", str(post_path), "--strict"])
    assert rc == 0


def test_flag_mismatch_raises_clear_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Pre + post that reference different flags -> exit 2 with an action marker."""
    pre = _baseline(flag="flag_a")
    post = _baseline(flag="flag_b")
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)
    rc = main(["--pre", str(pre_path), "--post", str(post_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "flag mismatch" in err
    assert "fix:" in err


def test_json_mode_emits_structured_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json prints a parseable JSON document with the gates list + overall."""
    pre = _baseline()
    post = copy.deepcopy(pre)
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)
    rc = main(["--pre", str(pre_path), "--post", str(post_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["flag"] == "test_flag"
    assert payload["overall"] == "ALL GATES PASS"
    names = {g["name"] for g in payload["gates"]}
    assert names == {"state", "eval", "latency", "sample_journey"}


def test_strict_mode_exits_inconclusive_on_partial_eval_capture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial eval capture on either side makes the eval gate
    INCONCLUSIVE; ``--strict`` exits 3 (the shared inconclusive code).

    Sabotage-proof: drop the ``partial`` check in ``_check_gate_benchmark`` —
    the partial suite is silently skipped, the gate passes and --strict
    exits 0. Restored.
    """
    pre = _baseline()
    post = copy.deepcopy(pre)
    post["eval"]["reflib"] = {"partial": True, "judge_failures": 2}
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    _write_baseline(pre_path, pre)
    _write_baseline(post_path, post)

    rc = main(["--pre", str(pre_path), "--post", str(post_path), "--strict"])
    assert rc == 3
    out = capsys.readouterr().out
    assert "[INCONCLUSIVE] eval" in out
    assert "INCONCLUSIVE — partial capture" in out
