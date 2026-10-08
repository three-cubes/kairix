"""
CLI for kairix eval — automated evaluation suite generation and monitoring.

Subcommands:
  generate    Generate a new benchmark suite using the GPL pipeline
  enrich      Enrich an existing suite with graded gold_titles
  monitor     Run canary suite and check for regression
  report      Generate markdown report from monitor log
  chunk-stats Per-source-type chunk-size distribution (ADR-028 measurement)

Usage:
  kairix eval generate --output suites/generated.yaml --count 100
  kairix eval enrich --suite suites/v2-real-world.yaml --output suites/v2-enriched.yaml
  kairix eval monitor --suite suites/canary.yaml
  kairix eval report --days 30
  kairix eval chunk-stats   # resolves the deployment's index automatically
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.quality.completeness import (
    EXIT_INCONCLUSIVE,
    judge_failures,
    partial_diagnostic,
    partial_warning,
)

_DEFAULT_DEPLOYMENT = "gpt-4o-mini"
_DEFAULT_AGENT = "shape"
_AGENT_HELP = "Agent for retrieval scoping (default: shape)"
_DB_HELP = "kairix SQLite index path (default: the deployment's index, e.g. /var/lib/kairix/index.sqlite)"


def _resolve_db_path(explicit: str | None) -> str:
    """Resolve the eval index path (#552).

    Returns the explicit ``--db`` value when given; otherwise the
    deployment's canonical index via :func:`kairix.paths.index_path`
    (``/var/lib/kairix/index.sqlite`` on a system/container install, the
    XDG data dir on a user install) — never a hardcoded ``~/.cache``
    path that only exists on a dev laptop, so ``kairix eval`` works
    out-of-the-box on a standard docker/system deployment.
    """
    if explicit:
        return explicit
    from kairix.paths import index_path

    return str(index_path())


# F17 — argparse action keyword repeated across boolean-flag declarations; one
# constant keeps the well-known sentinel in a single edit site.
_STORE_TRUE = "store_true"


# ---------------------------------------------------------------------------
# Production collaborators — lazily imported so ``kairix eval --help`` (and
# every subcommand that doesn't need them) never pays for the heavy eval
# modules. Bound onto :class:`EvalCliDeps` via ``default_factory``.
# ---------------------------------------------------------------------------


def _default_suite_generator() -> Any:
    """SuiteGenerator with its default protocol implementations."""
    from kairix.quality.eval.generate import SuiteGenerator

    return SuiteGenerator()


def _default_gold_builder() -> Any:
    """GoldBuilder with its default protocol implementations."""
    from kairix.quality.eval.gold_builder import GoldBuilder

    return GoldBuilder()


def _default_run_monitor(**kwargs: Any) -> Any:
    from kairix.quality.eval.monitor import run_monitor

    return run_monitor(**kwargs)


def _default_generate_report(**kwargs: Any) -> str:
    from kairix.quality.eval.monitor import generate_report

    return generate_report(**kwargs)


def _default_hybrid_configs() -> list[Any]:
    from kairix.quality.eval.hybrid_sweep import build_default_configs

    return list(build_default_configs())


def _default_sweep_hybrid(**kwargs: Any) -> Any:
    from kairix.quality.eval.hybrid_sweep import sweep_hybrid_params

    return sweep_hybrid_params(**kwargs)


def _default_sweep_bm25(**kwargs: Any) -> Any:
    from kairix.quality.eval.sweep import sweep_bm25_params

    return sweep_bm25_params(**kwargs)


def _default_index_db_path() -> Any:
    from kairix.core.db import get_db_path

    return get_db_path()


def _default_open_db(path: Path) -> Any:
    from kairix.core.db import open_db

    return open_db(path)


def _default_analyse_corpus(db: Any) -> Any:
    from kairix.quality.eval.auto_gold import analyse_corpus

    return analyse_corpus(db)


def _default_template_queries(profile: Any, n: int) -> list[dict[str, Any]]:
    from kairix.quality.eval.auto_gold import generate_template_queries

    return list(generate_template_queries(profile, n=n))


def _default_build_suite(queries: list[dict[str, Any]], path: str) -> None:
    from kairix.quality.eval.auto_gold import build_suite

    build_suite(queries, path)


def _default_run_gate(scores: dict[str, float], **kwargs: Any) -> Any:
    from kairix.quality.eval.gate import run_gate

    return run_gate(scores, **kwargs)


@dataclass
class EvalCliDeps:
    """Injectable collaborators for the ``kairix eval`` subcommands.

    Canonical Deps shape (``kairix/worker.py::WorkerDeps``): production calls
    :func:`main` without ``deps`` and every field's ``default_factory`` binds
    the real (lazily imported) implementation. Tests construct
    ``EvalCliDeps(new_suite_generator=lambda: fake, ...)`` to drive each
    subcommand's parsing + result-mapping logic without Azure, hybrid search
    or the real index.

    - ``new_suite_generator`` / ``new_gold_builder``: zero-arg constructors.
    - ``run_monitor`` / ``generate_report``: the monitor module entry points.
    - ``hybrid_configs`` / ``sweep_hybrid`` / ``sweep_bm25``: the sweep engines.
    - ``index_db_path`` / ``open_db``: the deployment index (auto-gold, tune,
      gate corpus hints).
    - ``analyse_corpus`` / ``template_queries`` / ``build_suite``: the
      auto-gold corpus pipeline.
    - ``run_gate``: the KFEAT-013 quality gate.
    """

    new_suite_generator: Callable[[], Any] = field(default_factory=lambda: _default_suite_generator)
    new_gold_builder: Callable[[], Any] = field(default_factory=lambda: _default_gold_builder)
    run_monitor: Callable[..., Any] = field(default_factory=lambda: _default_run_monitor)
    generate_report: Callable[..., str] = field(default_factory=lambda: _default_generate_report)
    hybrid_configs: Callable[[], list[Any]] = field(default_factory=lambda: _default_hybrid_configs)
    sweep_hybrid: Callable[..., Any] = field(default_factory=lambda: _default_sweep_hybrid)
    sweep_bm25: Callable[..., Any] = field(default_factory=lambda: _default_sweep_bm25)
    index_db_path: Callable[[], Any] = field(default_factory=lambda: _default_index_db_path)
    open_db: Callable[[Path], Any] = field(default_factory=lambda: _default_open_db)
    analyse_corpus: Callable[[Any], Any] = field(default_factory=lambda: _default_analyse_corpus)
    template_queries: Callable[[Any, int], list[dict[str, Any]]] = field(
        default_factory=lambda: _default_template_queries
    )
    build_suite: Callable[[list[dict[str, Any]], str], None] = field(default_factory=lambda: _default_build_suite)
    run_gate: Callable[..., Any] = field(default_factory=lambda: _default_run_gate)


def _cmd_generate(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    print(f"Generating {args.count} benchmark cases → {args.output}")
    if not args.no_calibrate:
        print("Running calibration anchors...")

    # Production: SuiteGenerator with default protocol implementations
    # (LLMJudge wrapping ProviderEvalChatBackend; default Retriever;
    # default QueryGenerator). Tests inject a fake via EvalCliDeps.
    suite_gen = deps.new_suite_generator()
    result = suite_gen.generate_suite(
        db_path=_resolve_db_path(args.db),
        output_path=args.output,
        n_cases=args.count,
        categories=args.categories.split(",") if args.categories else None,
        deployment=args.deployment,
        calibrate_first=not args.no_calibrate,
        seed=args.seed,
        agent=args.agent,
    )

    if not result.calibration_passed and not args.no_calibrate:
        print("ERROR: Calibration failed. Use --no-calibrate to skip.", file=sys.stderr)
        for e in result.errors:
            print(f"  {e}", file=sys.stderr)
        return 1

    print("\nResults:")
    print(f"  Accepted: {result.n_accepted}")
    print(f"  Rejected (no grade-2 doc): {result.n_rejected}")
    print(f"  Failed (retrieval/API error): {result.n_failed}")
    print("\nCategory distribution:")
    for cat, count in sorted(result.category_counts.items()):
        print(f"  {cat}: {count}")

    if result.errors:
        print("\nWarnings:")
        for e in result.errors:
            print(f"  {e}")

    print(f"\nOutput: {result.output_path}")
    return 0


def _cmd_enrich(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    print(f"Enriching {args.suite} → {args.output}")
    print("Running hybrid search + LLM judge for each case...")

    suite_gen = deps.new_suite_generator()
    result = suite_gen.enrich_suite(
        suite_path=args.suite,
        output_path=args.output,
        db_path=_resolve_db_path(args.db),
        deployment=args.deployment,
        agent=args.agent,
    )

    print("\nResults:")
    print(f"  Total cases: {result.n_cases}")
    print(f"  Enriched with gold_titles: {result.n_enriched}")
    print(f"  Skipped (no relevant doc found): {result.n_skipped}")
    print(f"  Failed (retrieval error): {result.n_failed}")

    if result.errors:
        print("\nWarnings:")
        for e in result.errors:
            print(f"  {e}")

    print(f"\nOutput: {result.output_path}")
    return 0


def _cmd_monitor(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    print(f"Running canary monitor on {args.suite}...")

    result = deps.run_monitor(
        suite_path=args.suite,
        log_path=args.log,
        alert_threshold=args.alert_threshold,
        window_days=args.window_days,
        agent=args.agent,
    )

    failures = int(getattr(result, "judge_failures", 0) or 0)
    if failures:
        print(partial_diagnostic("this canary run (not recorded in the trend log)", failures), file=sys.stderr)
        return EXIT_INCONCLUSIVE

    print(f"\nMonitor result ({result.ts[:19]}):")
    print(f"  Cases run: {result.n_cases}")
    print(f"  Weighted NDCG: {result.weighted_ndcg:.4f}")
    print(f"  Vec failed: {result.vec_failed_count}")
    print("\nCategory NDCG:")
    for cat, score in sorted(result.ndcg_by_category.items()):
        print(f"  {cat}: {score:.4f}")

    if result.regression:
        print(f"\n⚠️  REGRESSION DETECTED: {result.regression_detail}", file=sys.stderr)
        if args.log:
            print(f"  Log: {args.log}")
        return 2  # distinct exit code for regression (vs hard failure)

    print("\n✓ No regression detected.")
    return 0


def _cmd_report(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    report = deps.generate_report(log_path=args.log, days=args.days)

    if args.output:
        from pathlib import Path

        # CLI trust boundary: --output is user-supplied. The kairix CLI runs
        # with the calling user's filesystem permissions and operates inside
        # the local-process trust model; the user can already write anywhere
        # their account permits via shell redirection. The trailing
        # suppressions below cover the path resolution and the write where
        # S2083 fires.
        output_path = Path(args.output).expanduser().resolve()  # NOSONAR — CLI trust boundary; see comment above
        if not output_path.parent.exists():
            print(
                f"Error: parent directory {output_path.parent} does not exist",
                file=sys.stderr,
            )
            return 1
        output_path.write_text(report, encoding="utf-8")  # NOSONAR — CLI trust boundary, see comment above
        print(f"Report written to {output_path}")
    else:
        print(report)

    return 0


def _cmd_build_gold(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    systems = [s.strip() for s in args.systems.split(",")]
    print(f"Building independent gold suite: {args.suite} → {args.output}")
    print(f"Systems: {systems}")
    print(f"Judge runs: {args.judge_runs}")

    # Production: GoldBuilder with default protocol implementations
    # (LLMJudge wrapping ProviderEvalChatBackend; default Retriever
    # wrapping the production hybrid-search pipeline). Tests inject a
    # fake via EvalCliDeps.
    gold_builder = deps.new_gold_builder()
    report = gold_builder.build_independent_gold(
        suite_path=Path(args.suite),
        output_path=Path(args.output),
        systems=systems,
        judge_runs=args.judge_runs,
        calibrate_first=not args.no_calibrate,
        limit_per_system=args.limit,
    )

    print("\nGold suite built:")
    print(f"  Queries: {report.queries_processed}")
    print(f"  Candidates pooled: {report.total_candidates_pooled}")
    print(f"  Avg candidates/query: {report.avg_candidates_per_query:.1f}")
    print(f"  Judge calls: {report.total_judge_calls}")
    print(
        f"  Grades: 2={report.grade_distribution.get(2, 0)} 1={report.grade_distribution.get(1, 0)} 0={report.grade_distribution.get(0, 0)}"
    )
    print(f"  Output: {args.output}")
    return 0


def _cmd_hybrid_sweep(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    configs = deps.hybrid_configs()
    if args.quick:
        # Quick mode: baselines + key hybrid variants + bm25_primary
        configs = [
            c
            for c in configs
            if c.name
            in (
                "bm25-only",
                "hybrid-k20-minimal",
                "hybrid-k40-minimal",
                "hybrid-k60-minimal",
                "hybrid-k60-defaults",
                "bm25primary-v5",
                "bm25primary-v10",
                "bm25primary-v20",
            )
        ]

    print(f"Running hybrid calibration sweep: {len(configs)} configs x suite {args.suite}")

    collection = getattr(args, "collection", None)
    collections_override = [collection] if collection else None

    report = deps.sweep_hybrid(
        suite_path=Path(args.suite),
        output_path=Path(args.output) if args.output else None,
        configs=configs,
        collections_override=collections_override,
    )

    print(f"\nSweep complete: {report.total_configs} configs, {report.total_duration_s:.0f}s")
    if report.best:
        b = report.best
        c = b.config
        print(f"\n{'=' * 70}")
        print("BEST CONFIG:")
        print(f"  Name: {c.name}")
        print(f"  Mode: {c.mode} | RRF k={c.rrf_k}")
        print(f"  Entity: {c.entity_enabled} (factor={c.entity_factor}, cap={c.entity_cap})")
        print(f"  Procedural: {c.procedural_enabled} (factor={c.procedural_factor})")
        print(f"  BM25 limit={c.bm25_limit} | Vec limit={c.vec_limit}")
        print(f"  Weighted total: {b.weighted_total:.4f}")
        print(f"  NDCG@10: {b.ndcg_at_10:.4f}")
        print(f"  Hit@5: {b.hit_at_5:.3f}")
        print(f"  MRR@10: {b.mrr_at_10:.4f}")
        print(f"  Vec failures: {b.n_vec_failed}/{b.n_cases}")
        print(f"  Avg latency: {b.avg_latency_ms:.0f}ms")
        print(f"{'=' * 70}")

    # Show top 10
    print("\nTop 10 configs:")
    for i, r in enumerate(report.results[:10], 1):
        print(
            f"  {i:2d}. {r.config.name:30s} → weighted={r.weighted_total:.4f} "
            f"NDCG={r.ndcg_at_10:.4f} Hit@5={r.hit_at_5:.3f} "
            f"vecfail={r.n_vec_failed}"
        )

    if args.output:
        print(f"\nFull results: {args.output}")

    return 0


def _cmd_auto_gold(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    try:
        db_path = deps.index_db_path()
    except FileNotFoundError:  # pragma: no cover — defensive; ``get_db_path`` does not raise FileNotFoundError under any production configuration (it always returns a Path, existing or not)
        print("ERROR: kairix index not found. Run 'kairix embed' first.", file=sys.stderr)
        return 1

    db = deps.open_db(Path(db_path))
    profile = deps.analyse_corpus(db)
    db.close()

    print(f"Corpus: {profile.total_docs} documents across {len(profile.collections)} collections")
    print(
        f"  Procedural: {profile.procedural_count}  Date files: {profile.date_filename_count}  Entity: {profile.entity_doc_count}"
    )

    queries = deps.template_queries(profile, args.count)
    print(f"\nGenerated {len(queries)} evaluation queries")

    # Show category distribution
    cats: dict[str, int] = {}
    for q in queries:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    for cat, n in sorted(cats.items()):
        print(f"  {cat}: {n}")

    output = args.output or "suites/auto-gold.yaml"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    deps.build_suite(queries, output)
    print(f"\nSuite written to: {output}")
    print(f"Next: kairix eval build-gold --suite {output} --output {output.replace('.yaml', '-graded.yaml')}")
    return 0


def _corpus_hints(deps: EvalCliDeps) -> Any:
    """Best-effort :class:`CorpusHints` from the deployment index, or ``None``.

    Shared by ``tune`` and ``gate``; any failure to open / analyse the index
    yields ``None`` so the caller falls back to generic hints.
    """
    from kairix.quality.eval.tune import CorpusHints

    try:
        db = deps.open_db(Path(deps.index_db_path()))
        profile = deps.analyse_corpus(db)
        db.close()
    except Exception:
        return None
    return CorpusHints(
        has_date_files=profile.date_filename_count > 0,
        has_procedural_docs=profile.procedural_count > 0,
        has_entity_folders=profile.entity_doc_count > 0,
    )


def _cmd_tune(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    import json

    from kairix.quality.eval.tune import CorpusHints, analyse_results, recommend

    # Load benchmark result
    try:
        with open(args.result) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    scores = data.get("summary", {}).get("category_scores", {})
    if not scores:
        print("ERROR: No category_scores found in result file.", file=sys.stderr)
        return 1

    analysis = analyse_results(scores, floor=args.floor)

    failures = judge_failures(data)
    if failures:
        print(partial_warning(f"benchmark result {args.result} (tuning advice may be skewed)", failures))

    print(f"Category scores (floor={args.floor}):")
    for cat, score in sorted(scores.items()):
        marker = "  " if score >= args.floor else "!!"
        print(f"  {marker} {cat:12s} {score:.3f}")

    if not analysis.weak_categories:
        print("\nAll categories above floor. No tuning needed.")
        return 0

    print(f"\nWeak categories: {', '.join(analysis.weak_categories)}")

    # Build corpus hints from the index if available
    hints = _corpus_hints(deps)
    if hints is None:
        print("  (index not available — using generic recommendations)")
        hints = CorpusHints()

    recs = recommend(analysis.weak_categories, hints)
    if recs:
        print("\nRecommendations:")
        for r in recs:
            print(f"\n  [{r.parameter}] {r.action}")
            print(f"    Reason: {r.reason}")
            print(f"    Expected: {r.expected_impact}")
    else:
        print("\nNo specific recommendations. Consider running a hybrid sweep.")

    return 0


def _cmd_gate(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    """Stage 5 of KFEAT-013 onboarding: read a benchmark result and apply
    the quality gate. Exits 0 on PASS, 2 on HOLD (so wrappers can chain on
    success). Argument schema mirrors ``eval tune`` deliberately: same
    --result, same --floor.
    """
    import json

    from kairix.quality.eval.tune import CorpusHints

    try:
        with open(args.result) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    failures = judge_failures(data)
    if failures:
        print(partial_diagnostic(f"benchmark result {args.result}", failures), file=sys.stderr)
        return EXIT_INCONCLUSIVE

    summary = data.get("summary", {})
    scores = summary.get("category_scores", {})
    weighted_total = float(summary.get("weighted_total", 0.0))
    if not scores:
        print("ERROR: No category_scores found in result file.", file=sys.stderr)
        return 1

    # Best-effort corpus hints from the local index — same approach as tune.
    # No index available → no hints; recommendations stay generic.
    hints = _corpus_hints(deps) or CorpusHints()

    result = deps.run_gate(scores, weighted_total=weighted_total, hints=hints, floor=args.floor)
    print(result.format())

    return 0 if result.passed else 2


def _cmd_chunk_stats(args: argparse.Namespace, _deps: EvalCliDeps) -> int:
    """ADR-028 §"Quality evaluation" #4 — emit per-source-type chunk-size stats.

    Reads ``content_vectors`` joined against ``documents`` / ``content``
    and emits mean / p50 / p95 / p99 chunk size per source type
    (markdown, pptx, pdf, docx, xlsx, email, calendar). Operator-facing
    diagnostic for spotting chunker fragmentation or over-uniformity.
    """
    from kairix.quality.eval.chunk_stats import emit_chunk_stats

    db_path = Path(_resolve_db_path(args.db_path)).expanduser()
    return emit_chunk_stats(db_path, sys.stdout)


def _cmd_sweep(args: argparse.Namespace, deps: EvalCliDeps) -> int:
    print(f"Sweeping BM25 parameters against: {args.suite}")

    report = deps.sweep_bm25(
        suite_path=Path(args.suite),
        output_path=Path(args.output) if args.output else None,
    )

    print(f"\nSweep complete: {report.total_configs} configs, {report.total_duration_s:.0f}s")
    if report.best:
        b = report.best
        print(f"\n{'=' * 60}")
        print("BEST CONFIG:")
        print(f"  Weights: filepath={b.weights[0]} title={b.weights[1]} doc={b.weights[2]}")
        print(f"  Query style: {b.query_style}")
        print(f"  Weighted total: {b.weighted_total:.4f}")
        print(f"  NDCG@10: {b.ndcg_at_10:.4f}")
        print(f"  Hit@5: {b.hit_at_5:.4f}")
        print(f"  MRR@10: {b.mrr_at_10:.4f}")
        print(f"{'=' * 60}")

    # Show top 5
    print("\nTop 5 configs:")
    for i, r in enumerate(report.results[:5], 1):
        print(
            f"  {i}. w=({r.weights[0]},{r.weights[1]},{r.weights[2]}) style={r.query_style:7s} → {r.weighted_total:.4f}"
        )

    if args.output:
        print(f"\nFull results: {args.output}")

    return 0


def main(argv: list[str] | None = None, *, deps: EvalCliDeps | None = None) -> None:
    """Parse ``argv`` and dispatch the ``kairix eval`` subcommand.

    ``deps`` (:class:`EvalCliDeps`) carries the subcommands' collaborators;
    production omits it and the real implementations are bound.
    """
    parser = argparse.ArgumentParser(
        prog="kairix eval",
        description="Automated evaluation suite generation and monitoring",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- generate ---
    p_gen = subparsers.add_parser("generate", help="Generate a new benchmark suite using the GPL pipeline")
    p_gen.add_argument("--output", required=True, help="Output suite YAML path")
    p_gen.add_argument("--count", type=int, default=100, help="Target case count (default: 100)")
    p_gen.add_argument("--categories", help="Comma-separated categories (default: all)")
    p_gen.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    p_gen.add_argument(
        "--deployment",
        default=_DEFAULT_DEPLOYMENT,
        help="Azure deployment name override; ignored when the configured provider resolves its own model",
    )
    p_gen.add_argument("--no-calibrate", action=_STORE_TRUE, help="Skip calibration anchor check")
    p_gen.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    p_gen.add_argument("--agent", default=_DEFAULT_AGENT, help=_AGENT_HELP)

    # --- enrich ---
    p_enr = subparsers.add_parser("enrich", help="Enrich an existing suite with graded gold_titles")
    p_enr.add_argument("--suite", required=True, help="Input suite YAML path")
    p_enr.add_argument("--output", required=True, help="Output suite YAML path")
    p_enr.add_argument(
        "--db",
        default=None,
        help=_DB_HELP,
    )
    p_enr.add_argument(
        "--deployment",
        default=_DEFAULT_DEPLOYMENT,
        help="Azure deployment name override; ignored when the configured provider resolves its own model",
    )
    p_enr.add_argument("--agent", default=_DEFAULT_AGENT, help=_AGENT_HELP)

    # --- monitor ---
    p_mon = subparsers.add_parser("monitor", help="Run canary suite and check for regression")
    p_mon.add_argument("--suite", required=True, help="Canary suite YAML path")
    p_mon.add_argument(
        "--log",
        default=None,
        help="Monitor log path (default: KAIRIX_MONITOR_LOG or ~/.cache/kairix/monitor.jsonl)",
    )
    p_mon.add_argument(
        "--alert-threshold",
        type=float,
        default=0.05,
        help="Relative NDCG drop that triggers regression (default: 0.05)",
    )
    p_mon.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="Rolling window for baseline average in days (default: 7)",
    )
    p_mon.add_argument("--agent", default=_DEFAULT_AGENT, help=_AGENT_HELP)

    # --- report ---
    p_rep = subparsers.add_parser("report", help="Generate markdown report from monitor log")
    p_rep.add_argument(
        "--log",
        default=None,
        help="Monitor log path (default: KAIRIX_MONITOR_LOG or ~/.cache/kairix/monitor.jsonl)",
    )
    p_rep.add_argument("--days", type=int, default=30, help="Days of history to include (default: 30)")
    p_rep.add_argument("--output", default=None, help="Markdown output path (stdout if omitted)")

    # --- build-gold ---
    p_gold = subparsers.add_parser("build-gold", help="Build independent gold suite via TREC pooling + LLM judge")
    p_gold.add_argument("--suite", required=True, help="Input suite YAML (queries + categories)")
    p_gold.add_argument("--output", required=True, help="Output enriched suite YAML")
    p_gold.add_argument(
        "--systems",
        default="bm25-equal,bm25-filepath,bm25-title,vector",
        help="Retrieval systems to pool (default: bm25-equal,bm25-filepath,bm25-title,vector)",
    )
    p_gold.add_argument("--judge-runs", type=int, default=2, help="Judge runs per query (default: 2)")
    p_gold.add_argument("--no-calibrate", action=_STORE_TRUE, help="Skip judge calibration")
    p_gold.add_argument("--limit", type=int, default=10, help="Top-k per system (default: 10)")

    # --- auto-gold ---
    p_ag = subparsers.add_parser(
        "auto-gold",
        help="Generate evaluation suite from corpus analysis (no LLM needed)",
    )
    p_ag.add_argument(
        "--output",
        default=None,
        help="Output suite YAML path (default: suites/auto-gold.yaml)",
    )
    p_ag.add_argument("--count", type=int, default=50, help="Target query count (default: 50)")

    # --- tune ---
    p_tune = subparsers.add_parser("tune", help="Analyse benchmark results and recommend parameter tuning")
    p_tune.add_argument("--result", required=True, help="Benchmark result JSON file")
    p_tune.add_argument(
        "--floor",
        type=float,
        default=0.50,
        help="Category floor threshold (default: 0.50)",
    )

    # --- gate ---
    p_gate = subparsers.add_parser(
        "gate",
        help="Stage 5 (KFEAT-013) — apply quality gate to a benchmark result; exit 0 PASS / 2 HOLD",
    )
    p_gate.add_argument("--result", required=True, help="Benchmark result JSON file")
    p_gate.add_argument(
        "--floor",
        type=float,
        default=0.50,
        help="Category floor threshold (default: 0.50)",
    )

    # --- chunk-stats (ADR-028 §"Quality evaluation" #4) ---
    p_chunk = subparsers.add_parser(
        "chunk-stats",
        help="Per-source-type chunk-size distribution (mean, p50, p95, p99)",
    )
    p_chunk.add_argument(
        "--db-path",
        default=None,
        help=_DB_HELP,
    )

    # --- sweep ---
    p_sweep = subparsers.add_parser("sweep", help="Grid search BM25 column weights and query styles")
    p_sweep.add_argument("--suite", required=True, help="Benchmark suite YAML with gold_titles")
    p_sweep.add_argument("--output", default=None, help="CSV output path (stdout summary if omitted)")

    # --- hybrid-sweep ---
    p_hsweep = subparsers.add_parser(
        "hybrid-sweep",
        help="Grid search over hybrid pipeline: RRF k, boosts, retrieval modes",
    )
    p_hsweep.add_argument("--suite", required=True, help="Independent gold suite YAML")
    p_hsweep.add_argument("--output", default=None, help="CSV output path")
    p_hsweep.add_argument("--collection", default=None, help="Restrict search to this collection only")
    p_hsweep.add_argument(
        "--quick",
        action=_STORE_TRUE,
        help="Quick mode: run only baseline + key RRF k variants",
    )

    args = parser.parse_args(argv)

    # Resolve default log path for report — env read lives in kairix.paths (F4).
    if args.subcommand in ("monitor", "report") and args.log is None:
        from kairix.paths import monitor_log_path

        args.log = str(monitor_log_path())

    dispatch = {
        "generate": _cmd_generate,
        "enrich": _cmd_enrich,
        "monitor": _cmd_monitor,
        "report": _cmd_report,
        "build-gold": _cmd_build_gold,
        "auto-gold": _cmd_auto_gold,
        "tune": _cmd_tune,
        "gate": _cmd_gate,
        "sweep": _cmd_sweep,
        "hybrid-sweep": _cmd_hybrid_sweep,
        "chunk-stats": _cmd_chunk_stats,
    }

    fn = dispatch[args.subcommand]
    sys.exit(fn(args, deps if deps is not None else EvalCliDeps()))
