"""
CLI for kairix eval — automated evaluation suite generation and monitoring.

Subcommands:
  generate   Generate a new benchmark suite using the GPL pipeline
  enrich     Enrich an existing suite with graded gold_titles
  monitor    Run canary suite and check for regression
  report     Generate markdown report from monitor log

Usage:
  kairix eval generate --output suites/generated.yaml --count 100
  kairix eval enrich --suite suites/v2-real-world.yaml --output suites/v2-enriched.yaml
  kairix eval monitor --suite suites/canary.yaml
  kairix eval report --days 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DEFAULT_DB_PATH = str(Path.home() / ".cache/kairix/index.sqlite")
_DEFAULT_DEPLOYMENT = "gpt-4o-mini"
_DEFAULT_AGENT = "shape"
_AGENT_HELP = "Agent for retrieval scoping (default: shape)"


def _cmd_generate(args: argparse.Namespace) -> int:
    from kairix.quality.eval.generate import SuiteGenerator

    print(f"Generating {args.count} benchmark cases → {args.output}")
    if not args.no_calibrate:
        print("Running calibration anchors...")

    # Construct SuiteGenerator with default protocol implementations
    # (LLMJudge wrapping AzureChatBackend; default Retriever; default
    # QueryGenerator). Tests construct SuiteGenerator with FakeXxx fakes.
    suite_gen = SuiteGenerator()
    result = suite_gen.generate_suite(
        db_path=args.db,
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


def _cmd_enrich(args: argparse.Namespace) -> int:
    from kairix.quality.eval.generate import SuiteGenerator

    print(f"Enriching {args.suite} → {args.output}")
    print("Running hybrid search + LLM judge for each case...")

    suite_gen = SuiteGenerator()
    result = suite_gen.enrich_suite(
        suite_path=args.suite,
        output_path=args.output,
        db_path=args.db,
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


def _cmd_monitor(args: argparse.Namespace) -> int:
    from kairix.quality.eval.monitor import run_monitor

    print(f"Running canary monitor on {args.suite}...")

    result = run_monitor(
        suite_path=args.suite,
        log_path=args.log,
        alert_threshold=args.alert_threshold,
        window_days=args.window_days,
        agent=args.agent,
    )

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


def _cmd_report(args: argparse.Namespace) -> int:
    from kairix.quality.eval.monitor import generate_report

    report = generate_report(log_path=args.log, days=args.days)

    if args.output:
        from pathlib import Path

        # CLI trust boundary: --output is user-supplied. The kairix CLI runs
        # with the calling user's filesystem permissions and operates inside
        # the local-process trust model; the user can already write anywhere
        # their account permits via shell redirection. The NOSONAR is on the
        # statement that resolves the path because that's where S2083 fires.
        output_path = (
            Path(args.output).expanduser().resolve()
        )  # NOSONAR(python:S2083) — CLI trust boundary, see comment above
        if not output_path.parent.exists():
            print(
                f"Error: parent directory {output_path.parent} does not exist",
                file=sys.stderr,
            )
            return 1
        output_path.write_text(report, encoding="utf-8")
        print(f"Report written to {output_path}")
    else:
        print(report)

    return 0


def _cmd_build_gold(args: argparse.Namespace) -> int:
    from pathlib import Path

    from kairix.quality.eval.gold_builder import GoldBuilder

    systems = [s.strip() for s in args.systems.split(",")]
    print(f"Building independent gold suite: {args.suite} → {args.output}")
    print(f"Systems: {systems}")
    print(f"Judge runs: {args.judge_runs}")

    # Construct GoldBuilder with default protocol implementations
    # (LLMJudge wrapping AzureChatBackend; default Retriever wrapping
    # the production hybrid-search pipeline). Tests construct GoldBuilder
    # with FakeLLMJudge / FakeRetriever for isolation.
    gold_builder = GoldBuilder()
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


def _cmd_hybrid_sweep(args: argparse.Namespace) -> int:
    import logging
    from pathlib import Path

    from kairix.quality.eval.hybrid_sweep import (
        build_default_configs,
        sweep_hybrid_params,
    )

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    configs = build_default_configs()
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

    report = sweep_hybrid_params(
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


def _cmd_auto_gold(args: argparse.Namespace) -> int:
    from pathlib import Path

    from kairix.core.db import get_db_path, open_db
    from kairix.quality.eval.auto_gold import (
        analyse_corpus,
        build_suite,
        generate_template_queries,
    )

    try:
        db_path = get_db_path()
    except FileNotFoundError:
        print("ERROR: kairix index not found. Run 'kairix embed' first.", file=sys.stderr)
        return 1

    db = open_db(Path(db_path))
    profile = analyse_corpus(db)
    db.close()

    print(f"Corpus: {profile.total_docs} documents across {len(profile.collections)} collections")
    print(
        f"  Procedural: {profile.procedural_count}  Date files: {profile.date_filename_count}  Entity: {profile.entity_doc_count}"
    )

    queries = generate_template_queries(profile, n=args.count)
    print(f"\nGenerated {len(queries)} evaluation queries")

    # Show category distribution
    cats: dict[str, int] = {}
    for q in queries:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    for cat, n in sorted(cats.items()):
        print(f"  {cat}: {n}")

    output = args.output or "suites/auto-gold.yaml"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    build_suite(queries, output)
    print(f"\nSuite written to: {output}")
    print(f"Next: kairix eval build-gold --suite {output} --output {output.replace('.yaml', '-graded.yaml')}")
    return 0


def _cmd_tune(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

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

    print(f"Category scores (floor={args.floor}):")
    for cat, score in sorted(scores.items()):
        marker = "  " if score >= args.floor else "!!"
        print(f"  {marker} {cat:12s} {score:.3f}")

    if not analysis.weak_categories:
        print("\nAll categories above floor. No tuning needed.")
        return 0

    print(f"\nWeak categories: {', '.join(analysis.weak_categories)}")

    # Build corpus hints from the index if available
    hints = CorpusHints()
    try:
        from kairix.core.db import get_db_path, open_db
        from kairix.quality.eval.auto_gold import analyse_corpus

        db_path = get_db_path()
        db = open_db(Path(db_path))
        profile = analyse_corpus(db)
        db.close()
        hints = CorpusHints(
            has_date_files=profile.date_filename_count > 0,
            has_procedural_docs=profile.procedural_count > 0,
            has_entity_folders=profile.entity_doc_count > 0,
        )
    except Exception:
        print("  (index not available — using generic recommendations)")

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


def _cmd_gate(args: argparse.Namespace) -> int:
    """Stage 5 of KFEAT-013 onboarding: read a benchmark result and apply
    the quality gate. Exits 0 on PASS, 2 on HOLD (so wrappers can chain on
    success). Argument schema mirrors ``eval tune`` deliberately: same
    --result, same --floor.
    """
    import json
    from pathlib import Path

    from kairix.quality.eval.gate import run_gate
    from kairix.quality.eval.tune import CorpusHints

    try:
        with open(args.result) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    summary = data.get("summary", {})
    scores = summary.get("category_scores", {})
    weighted_total = float(summary.get("weighted_total", 0.0))
    if not scores:
        print("ERROR: No category_scores found in result file.", file=sys.stderr)
        return 1

    # Best-effort corpus hints from the local index — same approach as tune.
    hints = CorpusHints()
    try:
        from kairix.core.db import get_db_path, open_db
        from kairix.quality.eval.auto_gold import analyse_corpus

        db_path = get_db_path()
        db = open_db(Path(db_path))
        profile = analyse_corpus(db)
        db.close()
        hints = CorpusHints(
            has_date_files=profile.date_filename_count > 0,
            has_procedural_docs=profile.procedural_count > 0,
            has_entity_folders=profile.entity_doc_count > 0,
        )
    except Exception:
        # No index available — fall back to no hints. Recommendations stay generic.
        pass

    result = run_gate(scores, weighted_total=weighted_total, hints=hints, floor=args.floor)
    print(result.format())

    return 0 if result.passed else 2


def _cmd_sweep(args: argparse.Namespace) -> int:
    from pathlib import Path

    from kairix.quality.eval.sweep import sweep_bm25_params

    print(f"Sweeping BM25 parameters against: {args.suite}")

    report = sweep_bm25_params(
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


def main(argv: list[str] | None = None) -> None:
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
        default=_DEFAULT_DB_PATH,
        help="kairix SQLite path (default: ~/.cache/kairix/index.sqlite)",
    )
    p_gen.add_argument(
        "--deployment",
        default=_DEFAULT_DEPLOYMENT,
        help="Azure deployment (default: gpt-4o-mini)",
    )
    p_gen.add_argument("--no-calibrate", action="store_true", help="Skip calibration anchor check")
    p_gen.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    p_gen.add_argument("--agent", default=_DEFAULT_AGENT, help=_AGENT_HELP)

    # --- enrich ---
    p_enr = subparsers.add_parser("enrich", help="Enrich an existing suite with graded gold_titles")
    p_enr.add_argument("--suite", required=True, help="Input suite YAML path")
    p_enr.add_argument("--output", required=True, help="Output suite YAML path")
    p_enr.add_argument(
        "--db",
        default=_DEFAULT_DB_PATH,
        help="kairix SQLite path",
    )
    p_enr.add_argument(
        "--deployment",
        default=_DEFAULT_DEPLOYMENT,
        help="Azure deployment (default: gpt-4o-mini)",
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
    p_gold.add_argument("--no-calibrate", action="store_true", help="Skip judge calibration")
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
        action="store_true",
        help="Quick mode: run only baseline + key RRF k variants",
    )

    args = parser.parse_args(argv)

    # Resolve default log path for report
    if args.subcommand in ("monitor", "report") and args.log is None:
        import os
        from pathlib import Path

        args.log = os.environ.get("KAIRIX_MONITOR_LOG", str(Path.home() / ".cache/kairix/monitor.jsonl"))

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
    }

    fn = dispatch[args.subcommand]
    sys.exit(fn(args))
