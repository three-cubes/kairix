"""Diff pre-flip / post-flip baseline JSONs and check hard cutover gates.

Consumes two JSON envelopes produced by ``capture_baseline.py`` and
reports the per-gate delta + pass/fail per the cutover protocol in
``docs/architecture/feature-flag-architecture.md`` §4.2.

Hard gates (any failure -> rollback recommended):

  * state: per-collection doc_count + total_bytes within +-2%
  * benchmark recall: reflib recall_at_10 within +-2 percentage points;
                      LoCoMo recall within +-3 percentage points
  * latency: P95 within +-20%
  * sample_journey: >=80% of canonical queries retain >=3/5 of their
                    top-5 paths (intersection-over-original-5)

Surfaces that are ``null`` on either side are skipped with a "no data"
status — they neither pass nor fail (operator decides).

Usage::

    python scripts/cutover/diff_baseline.py \\
        --pre /tmp/baseline-obsidian-pre.json \\
        --post /tmp/baseline-obsidian-post.json --strict

By default the script always exits 0 so the operator can read the
report. ``--strict`` makes any gate failure exit non-zero so the cutover
can be wired into CI / a deployment pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kairix.quality.completeness import EXIT_INCONCLUSIVE, is_complete

# Hard-gate thresholds (per spec §4.2 Step 5).
STATE_TOLERANCE_PCT = 2.0
RECALL_TOLERANCE_PP = 2.0
LOCOMO_TOLERANCE_PP = 3.0
LATENCY_TOLERANCE_PCT = 20.0
JOURNEY_OVERLAP_REQ = 3  # of top-5
JOURNEY_PARITY_REQ = 0.80  # 80% of queries must hit the overlap req


@dataclass
class GateResult:
    """One gate's verdict + the numbers that justify it."""

    name: str
    status: str  # "pass" | "fail" | "skip"
    detail: str
    delta: dict[str, Any] = field(default_factory=dict)


def _load_baseline(path: Path) -> dict[str, Any]:
    """Load a baseline JSON envelope, raising a clean error if malformed."""
    if not path.exists():
        raise FileNotFoundError(f"baseline not found: {path}")
    with path.open() as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"baseline {path} is not a JSON object")
    return data


def _verify_same_flag(pre: dict[str, Any], post: dict[str, Any]) -> str:
    """Confirm pre + post reference the same flag. Returns the flag name."""
    pre_flag = pre.get("flag")
    post_flag = post.get("flag")
    if not isinstance(pre_flag, str) or not pre_flag:
        raise ValueError("pre baseline missing 'flag' field")
    if pre_flag != post_flag:
        raise ValueError(
            f"flag mismatch: pre={pre_flag!r}, post={post_flag!r}. "
            f"fix: re-run capture_baseline.py with --flag {pre_flag} on the post side."
        )
    return pre_flag


def _check_gate_state(pre: Any, post: Any) -> GateResult:
    """Per-collection counts within +-STATE_TOLERANCE_PCT."""
    if pre is None or post is None:
        return GateResult("state", "skip", "state surface missing on one side")
    pre_map = _index_collections(pre.get("per_collection") or [])
    post_map = _index_collections(post.get("per_collection") or [])
    deltas: list[dict[str, Any]] = []
    failures: list[str] = []
    for name in sorted(pre_map.keys() | post_map.keys()):
        delta = _compare_collection(name, pre_map.get(name), post_map.get(name))
        deltas.append(delta)
        if delta["status"] == "fail":
            failures.append(name)
    status = "fail" if failures else "pass"
    detail = (
        f"all {len(deltas)} collections within +-{STATE_TOLERANCE_PCT}%"
        if status == "pass"
        else f"{len(failures)} collection(s) exceed +-{STATE_TOLERANCE_PCT}%: {failures}"
    )
    digest_pre = pre.get("content_hash_digest")
    digest_post = post.get("content_hash_digest")
    return GateResult(
        "state",
        status,
        detail,
        delta={
            "per_collection": deltas,
            "content_hash_pre": digest_pre,
            "content_hash_post": digest_post,
            "content_hash_changed": digest_pre != digest_post,
        },
    )


def _index_collections(rows: list[Any]) -> dict[str, dict[str, int]]:
    """Index per-collection rows by collection name."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("collection")
        if not isinstance(name, str):
            continue
        out[name] = {
            "doc_count": int(row.get("doc_count") or 0),
            "total_bytes": int(row.get("total_bytes") or 0),
        }
    return out


def _compare_collection(name: str, pre: dict[str, int] | None, post: dict[str, int] | None) -> dict[str, Any]:
    """Compare one collection's counts; flag a violation if outside tolerance."""
    pre = pre or {"doc_count": 0, "total_bytes": 0}
    post = post or {"doc_count": 0, "total_bytes": 0}
    doc_pct = _pct_delta(pre["doc_count"], post["doc_count"])
    byte_pct = _pct_delta(pre["total_bytes"], post["total_bytes"])
    worst = max(abs(doc_pct), abs(byte_pct))
    status = "pass" if worst <= STATE_TOLERANCE_PCT else "fail"
    return {
        "collection": name,
        "pre": pre,
        "post": post,
        "doc_count_pct": round(doc_pct, 3),
        "total_bytes_pct": round(byte_pct, 3),
        "status": status,
    }


def _pct_delta(pre: float, post: float) -> float:
    """Percentage change from pre to post. Treats 0 -> non-zero as 100%."""
    if pre == 0 and post == 0:
        return 0.0
    if pre == 0:
        return 100.0
    return ((post - pre) / pre) * 100.0


def _check_gate_benchmark(pre: Any, post: Any) -> GateResult:
    """Recall scores within tolerance; reflib +-2pp, LoCoMo +-3pp."""
    if pre is None or post is None:
        return GateResult("eval", "skip", "eval surface missing on one side")
    partial = sorted(
        f"{side}.{suite}"
        for side, surface in (("pre", pre), ("post", post))
        for suite, payload in surface.items()
        if isinstance(payload, dict) and not is_complete(payload)
    )
    if partial:
        return GateResult(
            "eval",
            "inconclusive",
            f"partial eval capture (LLM-judge failures) in {partial} — re-run capture_baseline",
        )
    failures: list[str] = []
    details: dict[str, Any] = {}
    _check_recall_suite(pre, post, "reflib", "recall_at_10", RECALL_TOLERANCE_PP, details, failures)
    _check_recall_suite(pre, post, "locomo", "recall", LOCOMO_TOLERANCE_PP, details, failures)
    if not details:
        return GateResult("eval", "skip", "no recognised recall metrics in either baseline")
    status = "fail" if failures else "pass"
    detail = "all recall metrics within tolerance" if status == "pass" else f"recall regression(s): {failures}"
    return GateResult("eval", status, detail, delta=details)


def _check_recall_suite(
    pre: dict[str, Any],
    post: dict[str, Any],
    suite: str,
    metric: str,
    tolerance_pp: float,
    details: dict[str, Any],
    failures: list[str],
) -> None:
    """Record a single suite's recall delta + flag a failure if out of band."""
    pre_score = _extract_recall(pre.get(suite), metric)
    post_score = _extract_recall(post.get(suite), metric)
    if pre_score is None or post_score is None:
        return
    delta_pp = (post_score - pre_score) * 100.0
    status = "pass" if abs(delta_pp) <= tolerance_pp else "fail"
    details[suite] = {
        "metric": metric,
        "pre": pre_score,
        "post": post_score,
        "delta_pp": round(delta_pp, 3),
        "tolerance_pp": tolerance_pp,
        "status": status,
    }
    if status == "fail":
        failures.append(f"{suite}.{metric}")


def _extract_recall(payload: Any, metric: str) -> float | None:
    """Find ``metric`` in a possibly-nested benchmark payload."""
    if not isinstance(payload, dict):
        return None
    if metric in payload:
        return _coerce_float(payload[metric])
    for key in ("metrics", "summary", "scores", "aggregate"):
        nested = payload.get(key)
        if isinstance(nested, dict) and metric in nested:
            return _coerce_float(nested[metric])
    return None


def _coerce_float(value: Any) -> float | None:
    """Coerce a possibly-numeric value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _check_gate_latency(pre: Any, post: Any) -> GateResult:
    """P95 within +-LATENCY_TOLERANCE_PCT."""
    if pre is None or post is None:
        return GateResult("latency", "skip", "latency surface missing on one side")
    pre_p95 = _coerce_float(pre.get("p95_ms"))
    post_p95 = _coerce_float(post.get("p95_ms"))
    if pre_p95 is None or post_p95 is None:
        return GateResult("latency", "skip", "p95_ms missing on one side")
    delta_pct = _pct_delta(pre_p95, post_p95)
    status = "pass" if abs(delta_pct) <= LATENCY_TOLERANCE_PCT else "fail"
    detail = (
        f"P95 delta {delta_pct:+.2f}% (within +-{LATENCY_TOLERANCE_PCT}%)"
        if status == "pass"
        else f"P95 delta {delta_pct:+.2f}% exceeds +-{LATENCY_TOLERANCE_PCT}%"
    )
    return GateResult(
        "latency",
        status,
        detail,
        delta={
            "p50_pre": _coerce_float(pre.get("p50_ms")),
            "p50_post": _coerce_float(post.get("p50_ms")),
            "p95_pre": pre_p95,
            "p95_post": post_p95,
            "p95_pct": round(delta_pct, 3),
            "p99_pre": _coerce_float(pre.get("p99_ms")),
            "p99_post": _coerce_float(post.get("p99_ms")),
        },
    )


def _check_gate_sample_journey(pre: Any, post: Any) -> GateResult:
    """>=80% of canonical queries retain >=3/5 of their top-5 paths."""
    if pre is None or post is None:
        return GateResult("sample_journey", "skip", "sample_journey surface missing on one side")
    pre_map = _index_journey(pre)
    post_map = _index_journey(post)
    queries = sorted(pre_map.keys() & post_map.keys())
    if not queries:
        return GateResult("sample_journey", "skip", "no overlapping queries between pre and post")
    per_query, hits = _score_journey_overlap(queries, pre_map, post_map)
    parity = hits / len(queries) if queries else 0.0
    status = "pass" if parity >= JOURNEY_PARITY_REQ else "fail"
    detail = (
        f"{hits}/{len(queries)} queries kept >={JOURNEY_OVERLAP_REQ}/5 paths "
        f"(parity {parity:.0%}, required >={JOURNEY_PARITY_REQ:.0%})"
    )
    return GateResult(
        "sample_journey",
        status,
        detail,
        delta={"per_query": per_query, "parity": round(parity, 3)},
    )


def _index_journey(rows: Any) -> dict[str, list[str]]:
    """Index sample_journey rows by query string."""
    out: dict[str, list[str]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        query = row.get("query")
        paths = row.get("top_paths")
        if isinstance(query, str) and isinstance(paths, list):
            out[query] = [p for p in paths if isinstance(p, str)]
    return out


def _score_journey_overlap(
    queries: list[str],
    pre_map: dict[str, list[str]],
    post_map: dict[str, list[str]],
) -> tuple[list[dict[str, Any]], int]:
    """Score per-query overlap; return rows + count of queries meeting the bar."""
    per_query: list[dict[str, Any]] = []
    hits = 0
    for query in queries:
        pre_paths = pre_map[query][:5]
        post_paths = post_map[query][:5]
        kept = len(set(pre_paths) & set(post_paths))
        meets = kept >= JOURNEY_OVERLAP_REQ
        if meets:
            hits += 1
        per_query.append(
            {
                "query": query,
                "kept": kept,
                "of": len(pre_paths),
                "meets_bar": meets,
            }
        )
    return per_query, hits


def _format_human(report: dict[str, Any]) -> str:
    """Render the report as a plain-text table for the operator's eyes."""
    lines = [
        f"flag: {report['flag']}",
        f"pre  captured_at: {report['pre_captured_at']}",
        f"post captured_at: {report['post_captured_at']}",
        "",
        "gates:",
    ]
    for gate in report["gates"]:
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP", "inconclusive": "INCONCLUSIVE"}[gate["status"]]
        lines.append(f"  [{marker}] {gate['name']:<16}  {gate['detail']}")
    lines.append("")
    lines.append(f"overall: {report['overall']}")
    return "\n".join(lines)


def _build_report(pre: dict[str, Any], post: dict[str, Any]) -> dict[str, Any]:
    """Run every gate and assemble the report envelope."""
    flag = _verify_same_flag(pre, post)
    gates = [
        _check_gate_state(pre.get("state"), post.get("state")),
        _check_gate_benchmark(pre.get("eval"), post.get("eval")),
        _check_gate_latency(pre.get("latency"), post.get("latency")),
        _check_gate_sample_journey(pre.get("sample_journey"), post.get("sample_journey")),
    ]
    overall = _overall_verdict(gates)
    return {
        "flag": flag,
        "pre_captured_at": pre.get("captured_at"),
        "post_captured_at": post.get("captured_at"),
        "gates": [asdict(g) for g in gates],
        "overall": overall,
    }


def _overall_verdict(gates: list[GateResult]) -> str:
    """Combine per-gate statuses into the operator-facing summary."""
    failing = [g.name for g in gates if g.status == "fail"]
    if failing:
        return f"ROLLBACK RECOMMENDED — gate(s) failed: {', '.join(failing)}"
    inconclusive = [g.name for g in gates if g.status == "inconclusive"]
    if inconclusive:
        return f"INCONCLUSIVE — partial capture in gate(s): {', '.join(inconclusive)}; re-run capture_baseline."
    skipped = [g.name for g in gates if g.status == "skip"]
    if skipped and not [g for g in gates if g.status == "pass"]:
        return "INCONCLUSIVE — no gate produced a pass; re-run capture_baseline."
    if skipped:
        return f"ALL GATES PASS (skipped: {', '.join(skipped)})"
    return "ALL GATES PASS"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build + parse the CLI."""
    parser = argparse.ArgumentParser(
        prog="diff_baseline",
        description="Diff pre + post baselines from a feature-flag cutover.",
    )
    parser.add_argument("--pre", required=True, type=Path, help="Pre-flip baseline JSON.")
    parser.add_argument("--post", required=True, type=Path, help="Post-flip baseline JSON.")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON to stdout.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero if any hard gate fails (default: always 0).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 by default, non-zero under --strict on failure."""
    args = _parse_args(argv)
    try:
        pre = _load_baseline(args.pre)
        post = _load_baseline(args.post)
        report = _build_report(pre, post)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_human(report))
    any_failed = any(g["status"] == "fail" for g in report["gates"])
    if args.strict and any_failed:
        return 1
    if args.strict and any(g["status"] == "inconclusive" for g in report["gates"]):
        return EXIT_INCONCLUSIVE
    return 0


if __name__ == "__main__":  # pragma: no cover — module-as-script entrypoint
    sys.exit(main())
