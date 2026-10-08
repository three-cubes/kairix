"""Capture pre-flip / post-flip baseline state for a feature-flag cutover.

Used by the cutover protocol in
``docs/architecture/feature-flag-architecture.md`` §4.2. The operator runs
this script twice: once before flipping the flag, once after the soak
window. The output JSONs feed ``diff_baseline.py`` for hard-gate
verification.

This is operator-facing release-ops tooling — it lives under
``scripts/cutover/``, not in the ``kairix`` package (it is not shipped in
the published wheel).

CLI surfaces used (current, post-#377):

* **eval** — ``kairix benchmark run --suite <name> --output <dir>``.
  The unified benchmark CLI writes a JSON report named
  ``B-<suite-slug>-<system>-<YYYY-MM-DD>.json`` into the ``--output``
  directory. The capture script picks the freshest file in that
  directory and projects the report's ``summary`` block onto the
  ``{recall_at_10, ndcg_at_10, hit_rate_at_5, mrr_at_10, weighted_total}``
  shape the diff tool reads. ``recall_at_10`` mirrors ``ndcg_at_10``
  because the bundled gold suites use ``score_method: ndcg`` — NDCG@10
  is the canonical retrieval-quality surrogate.
* **latency** — ``kairix benchmark run --suite <name> --mode single-shot
  --output <dir>``. Single-shot mode populates
  ``diagnostics.per_query_runs[].latency_ms`` for every case; the
  capture script computes P50/P95/P99 from that array. (``--mode
  concurrent`` is a stub in the unified CLI today; single-shot
  exercises the same retrieval path with deterministic ordering.)
* **sample-journey** — ``kairix search --json <query>`` for each
  ``cutover.sample_queries`` entry in the operator config. When the
  config block is absent or empty the script falls back to a single
  canned probe ("default sample query") so the surface still emits a
  dedup digest.
* **state** — SQLite per-collection roll-up + content-hash digest;
  unchanged from the original capture.

Usage::

    python scripts/cutover/capture_baseline.py \\
        --flag obsidian_connector_primary \\
        --out /tmp/baseline-obsidian-pre.json

    # ... flip flag, soak ...

    python scripts/cutover/capture_baseline.py \\
        --flag obsidian_connector_primary \\
        --out /tmp/baseline-obsidian-post.json

    python scripts/cutover/diff_baseline.py \\
        --pre /tmp/baseline-obsidian-pre.json \\
        --post /tmp/baseline-obsidian-post.json --strict

Per-surface capture is resilient: if any individual surface fails
(missing CLI subcommand, error exit, unparseable JSON), the script
records a ``null`` for that surface and emits a warning. The diff tool
skips gates whose surface is missing.

JSON shape (frozen — diff_baseline depends on it):

    {
      "flag": "<flag-name>",
      "captured_at": "<ISO-8601 UTC>",
      "version": "<setuptools-scm version or 'unknown'>",
      "state": {
        "per_collection": [{"collection": str, "doc_count": int, "total_bytes": int}, ...],
        "content_hash_digest": "sha256:<hex>"
      } | null,
      "eval": {"reflib": {...}, "locomo": {...}} | null,
      "latency": {"p50_ms": float, "p95_ms": float, "p99_ms": float} | null,
      "sample_journey": [{"query": str, "top_paths": [str, ...]}, ...] | null
    }
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kairix.quality.completeness import judge_failures, partial_warning

logger = logging.getLogger("cutover.capture_baseline")

ALL_SURFACES = ("state", "eval", "latency", "sample-journey")

# Default benchmark suites consulted for the eval surface. ``reflib`` ships
# bundled; ``locomo`` is optional — when not present the suite-loader fails
# fast and the capture returns None for that key (the diff tool then skips
# the LoCoMo recall gate).
DEFAULT_EVAL_SUITES: tuple[str, ...] = ("reflib", "locomo")

# Default suite for the latency probe. Single-shot mode runs every case in
# the suite once, so a small suite keeps capture time bounded. ``reflib``
# is the canonical retrieval target so its latency is the right operator
# signal.
DEFAULT_LATENCY_SUITE: str = "reflib"

# Fallback sample-journey query used when the operator config carries no
# ``cutover.sample_queries`` block. Generic on purpose — its only role is
# to keep the sample-journey surface alive so the diff tool can compute a
# (degenerate) parity number against the same canned query on each side.
DEFAULT_SAMPLE_QUERY: str = "default sample query"


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_version() -> str:
    """Return the kairix package version, or 'unknown' if it can't be read."""
    try:
        from importlib.metadata import version

        return version("kairix")
    except Exception:
        return "unknown"


def _load_config(config_path: Path) -> dict[str, Any]:
    """Read a YAML config file. Returns {} on any failure (caller logs).

    Falls back gracefully if the file is missing — the diff tool handles
    missing sample-journey by skipping the gate.
    """
    if not config_path.exists():
        logger.warning("config not found at %s; sample-journey will be skipped", config_path)
        return {}
    try:
        import yaml  # type: ignore[import-untyped]  # PyYAML is a project dep
    except ImportError:
        logger.warning("PyYAML not available; cannot read config %s", config_path)
        return {}
    try:
        with config_path.open() as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            logger.warning("config %s is not a mapping; ignoring", config_path)
            return {}
        return data
    except Exception as exc:
        logger.warning("failed to parse config %s: %s", config_path, exc)
        return {}


def _resolve_sqlite_path(config: dict[str, Any]) -> Path | None:
    """Resolve the SQLite documents-db path from config."""
    paths_cfg = config.get("paths", {}) if isinstance(config.get("paths"), dict) else {}
    candidate = paths_cfg.get("documents_db") or paths_cfg.get("sqlite_path")
    if candidate:
        return Path(candidate)
    return None


def _capture_state_from_sqlite(db_path: Path) -> dict[str, Any] | None:
    """Capture per-collection counts + content-hash digest from a SQLite DB.

    Returns ``None`` if the database cannot be opened or queried.
    """
    if not db_path.exists():
        logger.warning("state: sqlite db not found at %s", db_path)
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        logger.warning("state: cannot open %s: %s", db_path, exc)
        return None
    try:
        per_collection = _query_per_collection(conn)
        digest = _query_content_hash_digest(conn)
    finally:
        conn.close()
    if per_collection is None or digest is None:
        return None
    return {"per_collection": per_collection, "content_hash_digest": digest}


def _query_per_collection(conn: sqlite3.Connection) -> list[dict[str, Any]] | None:
    """Run the per-collection roll-up query. Returns None on schema mismatch."""
    sql = (
        "SELECT d.collection, COUNT(*) AS doc_count, "
        "COALESCE(SUM(LENGTH(c.doc)), 0) AS total_bytes "
        "FROM documents d JOIN content c ON c.hash = d.hash "
        "WHERE d.active = 1 GROUP BY d.collection ORDER BY d.collection"
    )
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        logger.warning("state: per-collection query failed: %s", exc)
        return None
    return [{"collection": row[0], "doc_count": int(row[1]), "total_bytes": int(row[2])} for row in rows]


def _query_content_hash_digest(conn: sqlite3.Connection) -> str | None:
    """SHA-256 of the sorted concatenation of content hashes."""
    try:
        rows = conn.execute("SELECT hash FROM documents WHERE active = 1 ORDER BY hash").fetchall()
    except sqlite3.Error as exc:
        logger.warning("state: content-hash query failed: %s", exc)
        return None
    hasher = hashlib.sha256()
    for (h,) in rows:
        if h is None:
            continue
        hasher.update(h.encode("utf-8") if isinstance(h, str) else bytes(h))
        hasher.update(b"\n")
    return f"sha256:{hasher.hexdigest()}"


def _run_cli(
    argv: list[str],
    timeout: int = 600,
) -> subprocess.CompletedProcess[str] | None:
    """Invoke a CLI command. Returns the completed process or None on error.

    The benchmark CLI emits its JSON report to ``--output`` and writes a
    human-readable summary to stdout; callers that need the report file
    do not need stdout-as-JSON. ``_run_cli_json`` (below) is the legacy
    helper kept for sample-journey, where ``kairix search --json`` does
    emit a JSON envelope on stdout.
    """
    try:
        result = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("subprocess %s failed: %s", argv[:3], exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "subprocess %s exited %d: stderr=%s",
            argv[:3],
            result.returncode,
            (result.stderr or "")[:400],
        )
        return None
    return result


def _run_cli_json(argv: list[str], timeout: int = 600) -> dict[str, Any] | None:
    """Invoke a CLI command, parse stdout as JSON. Returns None on any failure.

    Used for ``kairix search --json`` (sample-journey). The benchmark
    surface uses :func:`_run_cli` instead because the report lives in
    ``--output`` rather than on stdout.
    """
    result = _run_cli(argv, timeout=timeout)
    if result is None:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.warning("subprocess %s emitted non-JSON: %s", argv[:3], exc)
        return None
    if not isinstance(parsed, dict):
        logger.warning("subprocess %s emitted non-object JSON", argv[:3])
        return None
    return parsed


def _load_benchmark_report(output_dir: Path, suite_name: str) -> dict[str, Any] | None:
    """Pick the freshest ``B-<suite-slug>-*.json`` report under ``output_dir``.

    Matches the naming the runner uses
    (``B-<suite-slug>-<system>-<YYYY-MM-DD>.json``) but tolerates any
    ``B-*.json`` so suite-slug variants (e.g. "reflib" vs
    "reflib-gold-v3") still resolve. Returns ``None`` if no matching
    file exists or the chosen file fails to parse.
    """
    if not output_dir.exists():
        return None
    # The suite-slug may differ from the operator's --suite argument
    # because the runner slugifies ``suite.meta.name``. Match any
    # ``B-*.json`` and take the freshest by mtime — the benchmark CLI
    # just wrote it, so it wins.
    candidates = sorted(output_dir.glob("B-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        logger.warning("eval: no benchmark report file in %s for suite %s", output_dir, suite_name)
        return None
    report_path = candidates[0]
    try:
        with report_path.open() as fh:
            parsed = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("eval: cannot read %s: %s", report_path, exc)
        return None
    if not isinstance(parsed, dict):
        logger.warning("eval: report at %s is not a JSON object", report_path)
        return None
    return parsed


def _project_eval_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Project a benchmark report's ``summary`` onto the diff-tool shape.

    The diff tool's recall gate reads ``payload["recall_at_10"]`` (or a
    fallback through ``metrics``/``summary``/``scores``/``aggregate``).
    The bundled gold suites use ``score_method: ndcg``, so NDCG@10 is
    the canonical retrieval-quality value — we surface it both as
    ``ndcg_at_10`` and as ``recall_at_10`` so the gate has a target. We
    also surface ``hit_rate_at_5``, ``mrr_at_10``, and
    ``weighted_total`` so an operator inspecting the JSON can read the
    full headline numbers.
    """
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    ndcg = _coerce_float(summary.get("ndcg_at_10"))
    hit5 = _coerce_float(summary.get("hit_rate_at_5"))
    mrr = _coerce_float(summary.get("mrr_at_10"))
    weighted_total = _coerce_float(summary.get("weighted_total"))
    payload: dict[str, Any] = {}
    if ndcg is not None:
        payload["ndcg_at_10"] = ndcg
        # diff_baseline._extract_recall reads "recall_at_10" first; map
        # NDCG@10 onto it so the recall gate has a value to compare.
        payload["recall_at_10"] = ndcg
        # LoCoMo's gate keys off the literal "recall" metric; surface it
        # so a future LoCoMo-bundled suite picks it up without further
        # changes here.
        payload["recall"] = ndcg
    if hit5 is not None:
        payload["hit_rate_at_5"] = hit5
    if mrr is not None:
        payload["mrr_at_10"] = mrr
    if weighted_total is not None:
        payload["weighted_total"] = weighted_total
    return payload


def _capture_one_benchmark_suite(suite: str, runner: _CLIRunner) -> dict[str, Any] | None:
    """Run ``kairix benchmark run --suite <suite> --output <tmp>`` and project.

    Returns the projected payload or ``None`` if the CLI failed, no
    report was emitted, or the summary block is empty.
    """
    with tempfile.TemporaryDirectory(prefix=f"kairix-baseline-{suite}-") as tmp:
        out_dir = Path(tmp)
        result = runner(
            ["kairix", "benchmark", "run", "--suite", suite, "--output", str(out_dir)],
            timeout=900,
        )
        if result is None:
            return None
        report = _load_benchmark_report(out_dir, suite)
        if report is None:
            return None
        failures = judge_failures(report)
        if failures:
            # Never record a partial run's scores as a baseline value; the
            # marker makes diff_baseline report the eval gate INCONCLUSIVE.
            logger.warning("eval: %s", partial_warning(f"suite {suite} (not captured)", failures))
            return {"partial": True, "judge_failures": failures}
        payload = _project_eval_payload(report)
        return payload or None


def _capture_benchmark_scores(
    suites: tuple[str, ...] = DEFAULT_EVAL_SUITES,
    runner: _CLIRunner | None = None,
) -> dict[str, Any] | None:
    """Capture eval scores from one or more benchmark suites.

    ``runner`` is the injection seam — tests pass a callable that emits a
    pre-canned report into the temporary output directory; production
    leaves it at None and falls back to :func:`_run_cli`.
    """
    runner = runner or _run_cli
    out: dict[str, Any] = {}
    for suite in suites:
        payload = _capture_one_benchmark_suite(suite, runner)
        if payload is not None:
            out[suite] = payload
    return out or None


def _capture_latency(
    suite: str = DEFAULT_LATENCY_SUITE,
    runner: _CLIRunner | None = None,
) -> dict[str, Any] | None:
    """Run ``kairix benchmark run --mode single-shot`` and pull p50/p95/p99.

    Single-shot mode populates ``diagnostics.per_query_runs[]`` with one
    row per case carrying ``latency_ms``. We compute p50/p95/p99 from
    that array. ``runner`` is the same injection seam as
    :func:`_capture_benchmark_scores`.
    """
    runner = runner or _run_cli
    with tempfile.TemporaryDirectory(prefix=f"kairix-baseline-latency-{suite}-") as tmp:
        out_dir = Path(tmp)
        result = runner(
            [
                "kairix",
                "benchmark",
                "run",
                "--suite",
                suite,
                "--mode",
                "single-shot",
                "--output",
                str(out_dir),
            ],
            timeout=900,
        )
        if result is None:
            return None
        report = _load_benchmark_report(out_dir, suite)
        if report is None:
            return None
        return _extract_latency_from_report(report)


def _extract_latency_from_report(report: dict[str, Any]) -> dict[str, Any] | None:
    """Compute p50/p95/p99 from a benchmark report's per-query latency rows.

    The single-shot dispatcher emits ``diagnostics.per_query_runs`` —
    one record per case with ``latency_ms`` (and ``latency_phase``).
    Falls back to scanning ``cases[].elapsed_ms`` when single-shot
    wasn't requested, so the legacy report shape still produces
    percentiles.
    """
    diagnostics = report.get("diagnostics", {}) if isinstance(report.get("diagnostics"), dict) else {}
    runs = diagnostics.get("per_query_runs")
    samples: list[float] = []
    if isinstance(runs, list):
        for row in runs:
            if not isinstance(row, dict):
                continue
            val = _coerce_float(row.get("latency_ms"))
            if val is not None:
                samples.append(val)
    if not samples:
        # Legacy fallback: every case carries elapsed_ms even without
        # single-shot mode. The runner emits one per case in the
        # "cases" array.
        cases = report.get("cases")
        if isinstance(cases, list):
            for row in cases:
                if not isinstance(row, dict):
                    continue
                val = _coerce_float(row.get("elapsed_ms"))
                if val is not None:
                    samples.append(val)
    if not samples:
        logger.warning("latency: no latency samples in benchmark report")
        return None
    samples.sort()
    return {
        "p50_ms": _percentile(samples, 0.50),
        "p95_ms": _percentile(samples, 0.95),
        "p99_ms": _percentile(samples, 0.99),
    }


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Linear-interpolation percentile on a pre-sorted sample list.

    Matches the convention numpy uses by default (``method='linear'``)
    so the numbers line up with operator expectations when they cross-
    check with a notebook.
    """
    if not sorted_samples:
        return 0.0
    if len(sorted_samples) == 1:
        return sorted_samples[0]
    pos = q * (len(sorted_samples) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_samples) - 1)
    frac = pos - lo
    return sorted_samples[lo] + (sorted_samples[hi] - sorted_samples[lo]) * frac


def _extract_latency_percentiles(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pick P50/P95/P99 fields out of an arbitrary payload (legacy helper).

    Retained because the unit-test suite at
    ``tests/cutover/test_capture_baseline.py`` exercises it directly to
    prove the diff-tool-facing latency shape. Production now flows
    through :func:`_extract_latency_from_report`.
    """
    candidates = [payload]
    for key in ("latency", "summary", "metrics", "stats"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    for src in candidates:
        p50 = _coerce_float(src.get("p50_ms") or src.get("p50"))
        p95 = _coerce_float(src.get("p95_ms") or src.get("p95"))
        p99 = _coerce_float(src.get("p99_ms") or src.get("p99"))
        if p50 is not None and p95 is not None and p99 is not None:
            return {"p50_ms": p50, "p95_ms": p95, "p99_ms": p99}
    logger.warning("latency: could not find p50/p95/p99 in payload")
    return None


def _coerce_float(value: Any) -> float | None:
    """Coerce a possibly-numeric value to float, or None."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _capture_sample_journey(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Run each canonical query and capture top-5 doc paths.

    Reads ``cutover.sample_queries`` from the operator config. When the
    block is absent or empty falls back to a single canned probe
    (:data:`DEFAULT_SAMPLE_QUERY`) so the surface still emits a row —
    that row is sufficient for the diff tool to compute parity if the
    same fallback fires on both pre and post sides.
    """
    queries = _resolve_sample_queries(config)
    results: list[dict[str, Any]] = []
    for query in queries:
        results.append({"query": query, "top_paths": _run_sample_query(query)})
    return results if results else None


def _resolve_sample_queries(config: dict[str, Any]) -> list[str]:
    """Return the sample-query list with a deterministic fallback.

    Looks at ``config["cutover"]["sample_queries"]``; if missing /
    non-list / empty, returns ``[DEFAULT_SAMPLE_QUERY]`` so the surface
    is never silently skipped. Operators get a one-line warning the
    first time the fallback fires so they know to populate the block.
    """
    cutover_cfg = config.get("cutover", {}) if isinstance(config.get("cutover"), dict) else {}
    queries = cutover_cfg.get("sample_queries")
    if isinstance(queries, list) and queries:
        cleaned = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if cleaned:
            return cleaned
    logger.info(
        "sample-journey: no cutover.sample_queries in config; using DEFAULT_SAMPLE_QUERY fallback. "
        "fix: add a 'cutover.sample_queries' list to kairix.config.yaml. "
        "next: see kairix.config.example.yaml for the canonical shape. "
        "run: python scripts/cutover/capture_baseline.py --help"
    )
    return [DEFAULT_SAMPLE_QUERY]


def _run_sample_query(
    query: str,
    runner: Callable[[list[str]], dict[str, Any] | None] | None = None,
) -> list[str]:
    """Invoke ``kairix search --json`` for a single query. Returns top-5 paths.

    ``runner`` is the dependency seam — tests pass an explicit callable
    so they never need to patch the module surface. The production path
    leaves ``runner=None``, which falls back to ``_run_cli_json``.
    """
    if runner is None:
        payload = _run_cli_json(["kairix", "search", "--json", query], timeout=120)
    else:
        payload = runner(["kairix", "search", "--json", query])
    if payload is None:
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    paths: list[str] = []
    for item in results[:5]:
        if not isinstance(item, dict):
            continue
        path = item.get("path") or item.get("source") or item.get("doc_path")
        if isinstance(path, str):
            paths.append(path)
    return paths


def _parse_surfaces(raw: str) -> list[str]:
    """Parse the ``--surface`` CSV. ``all`` expands to every surface."""
    items = [s.strip() for s in raw.split(",") if s.strip()]
    if not items or "all" in items:
        return list(ALL_SURFACES)
    invalid = [s for s in items if s not in ALL_SURFACES]
    if invalid:
        raise ValueError(f"unknown surface(s): {invalid}. fix: pick from {list(ALL_SURFACES)} or 'all'.")
    return items


def _build_baseline(
    flag: str,
    config_path: Path,
    surfaces: list[str],
    *,
    eval_suites: tuple[str, ...] = DEFAULT_EVAL_SUITES,
    latency_suite: str = DEFAULT_LATENCY_SUITE,
) -> dict[str, Any]:
    """Build the full baseline JSON envelope for the requested surfaces."""
    config = _load_config(config_path)
    envelope: dict[str, Any] = {
        "flag": flag,
        "captured_at": _now_iso(),
        "version": _read_version(),
    }
    if "state" in surfaces:
        envelope["state"] = _maybe_capture_state(config)
    if "eval" in surfaces:
        envelope["eval"] = _capture_benchmark_scores(eval_suites)
    if "latency" in surfaces:
        envelope["latency"] = _capture_latency(latency_suite)
    if "sample-journey" in surfaces:
        envelope["sample_journey"] = _capture_sample_journey(config)
    return envelope


def _maybe_capture_state(config: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve sqlite path from config and capture state, or return None."""
    db_path = _resolve_sqlite_path(config)
    if db_path is None:
        logger.warning("state: no paths.documents_db in config; skipping surface")
        return None
    return _capture_state_from_sqlite(db_path)


def _write_envelope(envelope: dict[str, Any], out_path: Path) -> None:
    """Write the envelope to ``out_path`` (parents created)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as fh:
        json.dump(envelope, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build + parse the CLI."""
    parser = argparse.ArgumentParser(
        prog="capture_baseline",
        description="Capture pre/post-flip baseline for a feature-flag cutover.",
    )
    parser.add_argument("--flag", required=True, help="Feature-flag name (snake_case).")
    parser.add_argument("--out", required=True, type=Path, help="Output JSON path.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("kairix.config.yaml"),
        help="Config file to read (default: kairix.config.yaml).",
    )
    parser.add_argument(
        "--surface",
        default="all",
        help="Comma-separated surfaces (state,eval,latency,sample-journey,all).",
    )
    parser.add_argument(
        "--eval-suites",
        default=",".join(DEFAULT_EVAL_SUITES),
        help=(
            f"Comma-separated benchmark suites for the eval surface "
            f"(default: {','.join(DEFAULT_EVAL_SUITES)}). Any unbundled suite "
            f"name is skipped with a warning."
        ),
    )
    parser.add_argument(
        "--latency-suite",
        default=DEFAULT_LATENCY_SUITE,
        help=(
            f"Benchmark suite for the latency surface (default: {DEFAULT_LATENCY_SUITE}). Single-shot mode is forced."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success (including partial captures)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    try:
        surfaces = _parse_surfaces(args.surface)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    eval_suites = tuple(s.strip() for s in args.eval_suites.split(",") if s.strip()) or DEFAULT_EVAL_SUITES
    envelope = _build_baseline(
        args.flag,
        args.config,
        surfaces,
        eval_suites=eval_suites,
        latency_suite=args.latency_suite,
    )
    _write_envelope(envelope, args.out)
    captured = [s for s in ALL_SURFACES if envelope.get(s.replace("-", "_")) is not None]
    print(f"captured {len(captured)}/{len(ALL_SURFACES)} surfaces ({','.join(captured) or 'none'}) -> {args.out}")
    return 0


# Type alias declared after the helpers so it can refer to subprocess.CompletedProcess.
_CLIRunner = Callable[..., "subprocess.CompletedProcess[str] | None"]


if __name__ == "__main__":  # pragma: no cover — module-as-script entrypoint
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
