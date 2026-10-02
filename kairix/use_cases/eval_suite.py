"""Operator-facing ``kairix eval`` use case — Plan B-parity Capability #4.

Runs the conversation-suite benchmark via :class:`SuiteRunner`, prints
the per-category breakdown to stdout (or JSON via ``--json``), and
optionally compares against a pinned baseline for the regression-gate
CI flow.

Compatibility surface: when ``argv[0]`` is one of the legacy
``kairix.quality.eval.cli`` subcommands (``generate`` / ``enrich`` /
``monitor`` / ``report`` / ``build-gold`` / ``auto-gold`` / ``gate`` /
``tune``), the use case forwards to that CLI so the existing operator
surface keeps working unchanged. Plan B-parity adds a NEW positional
``suite_path`` shape on top of the legacy subcommands.

Design contract:

- **Dependency injection is total.** ``fact_store`` / ``fact_extractor``
  / ``llm`` / ``paths`` are keyword-only kwargs on :func:`main`; tests
  inject fakes, production callers leave them ``None`` and the CLI
  resolves real implementations.
- **F1 clean.** No monkeypatching, no internal-attribute reassignment.
- **F26 clean.** Imports ``LLMBackend`` from
  ``kairix.platform.llm.protocol`` (not from a provider).
- **F21 clean.** Every actionable error carries ``fix:`` / ``next:``
  markers so the operator gets the correction action.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from kairix.core.facts.consolidation import ConsolidationPass
from kairix.core.protocols import (
    CorpusEmbedder,
    DocumentWriter,
    FactExtractor,
    FactStore,
)
from kairix.core.search.pipeline import SearchPipeline
from kairix.paths import KairixPaths
from kairix.platform.llm.protocol import LLMBackend
from kairix.quality.eval.suite_runner import SuiteResult, SuiteRunner

__all__ = ["main"]


# Subcommands that belong to the legacy ``kairix.quality.eval.cli`` surface.
# When ``argv[0]`` is one of these, the use case forwards the argv straight
# into the legacy CLI so the existing operator surface keeps working.
_LEGACY_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "generate",
        "enrich",
        "monitor",
        "report",
        "build-gold",
        "auto-gold",
        "gate",
        "tune",
        "hybrid-sweep",
        "sweep",
        # ADR-028 §"Quality evaluation" #4 — per-source-type chunk-size
        # distribution. Lives next to the other eval subcommands so the
        # operator surface stays one CLI.
        "chunk-stats",
    }
)

# Documented backends for the ``--backend`` flag.
_BACKENDS: tuple[str, ...] = ("kairix-native", "mem0")

# Maximum allowable regression — a baseline is "lost" only when the run
# falls more than this many percentage points below it. The threshold is
# documented in the Plan B-parity execution plan; aligns with the canary
# regression gate in ``kairix.quality.eval.monitor``.
_REGRESSION_TOLERANCE_PP: float = 2.0

# Prefix on every operator-facing error written to stderr. Extracted so
# the error envelope stays consistent and a rename has a single edit site.
_ERROR_PREFIX: str = "kairix eval: "


@dataclasses.dataclass(frozen=True)
class _ResolvedDeps:
    """Bundle of resolved dependencies for the suite-runner path."""

    paths: KairixPaths
    fact_store: FactStore
    fact_extractor: FactExtractor
    llm: LLMBackend
    # Plan B-parity D3 — optional SearchPipeline. When ``via_prep`` mode
    # is on (the new default), the suite runner routes queries through
    # this pipeline instead of calling ``fact_store.search`` directly.
    # ``None`` means legacy direct mode (regression-debugging escape).
    search_pipeline: SearchPipeline | None = None
    # Spike C1 Phase 3 — corpus-ingest collaborators. ``None``
    # preserves today's facts-only ingest behaviour; callers can
    # inject production wire-ups via the ``main()`` kwargs once
    # ``kairix.corpus.wiring`` lands its factories.
    document_writer: DocumentWriter | None = None
    embedder: CorpusEmbedder | None = None
    consolidation: ConsolidationPass | None = None


_SURFACE_HINT = (
    "hint: `kairix eval` runs conversation-eval suites (sessions + "
    "ground-truth queries against the fact extractor). For gold-suite "
    "retrieval benchmarks (reflib, contract, per-type-canary) use "
    "`kairix benchmark run --suite <name>`. The two surfaces are "
    "complementary — pick by suite shape, not by which CLI feels closer.\n"
)


def _emit_surface_hint(err_sink: TextIO) -> None:
    """Write the surface-disambiguation hint to ``err_sink``.

    Conversation-eval (this surface) and gold-suite benchmark
    (``kairix benchmark run``) are different paradigms. The hint tells
    operators which to pick when they reach for the wrong one.
    """
    err_sink.write(_SURFACE_HINT)


def main(
    argv: list[str] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    paths: KairixPaths | None = None,
    fact_store: FactStore | None = None,
    fact_extractor: FactExtractor | None = None,
    llm: LLMBackend | None = None,
    search_pipeline: SearchPipeline | None = None,
    document_writer: DocumentWriter | None = None,
    embedder: CorpusEmbedder | None = None,
    consolidation: ConsolidationPass | None = None,
) -> int:
    """CLI entry point for ``kairix eval``.

    Dispatches to:

    - The legacy :func:`kairix.quality.eval.cli.main` when ``argv[0]``
      is a legacy subcommand.
    - The Plan B-parity suite runner otherwise (positional
      ``suite_path``).

    Plan B-parity D3: by default the runner routes through the same
    :class:`SearchPipeline` ``kairix prep`` uses (``--via-prep``, the
    new default). The legacy direct ``fact_store.search`` path remains
    accessible via ``--legacy-direct`` for regression-debugging only.

    Emits a one-line surface-disambiguation hint pointing at
    ``kairix benchmark run`` for gold-suite work; conversation-eval
    stays on this surface (the two are not interchangeable — different
    suite shapes).
    """
    argv_list = list(argv if argv is not None else [])

    err_sink = err if err is not None else sys.stderr
    _emit_surface_hint(err_sink)

    if _is_legacy_subcommand(argv_list):
        return _dispatch_legacy(argv_list)

    out_sink = out if out is not None else sys.stdout

    args = _build_parser().parse_args(argv_list)
    suite_path = Path(args.suite_path)

    deps = resolve_deps(
        paths=paths,
        fact_store=fact_store,
        fact_extractor=fact_extractor,
        llm=llm,
        search_pipeline=search_pipeline,
        document_writer=document_writer,
        embedder=embedder,
        consolidation=consolidation,
        via_prep=args.via_prep,
        err_sink=err_sink,
    )
    if isinstance(deps, int):
        return deps

    return _execute_suite(args=args, suite_path=suite_path, deps=deps, out_sink=out_sink, err_sink=err_sink)


def _is_legacy_subcommand(argv_list: list[str]) -> bool:
    """True when ``argv[0]`` is one of the pre-existing eval subcommands."""
    return bool(argv_list) and argv_list[0] in _LEGACY_SUBCOMMANDS


def _dispatch_legacy(argv_list: list[str]) -> int:
    """Forward to the legacy ``kairix.quality.eval.cli`` entry point."""
    # Legacy passthrough: keep `kairix eval generate / enrich / monitor /
    # report / gate / tune / ...` working unchanged.
    from kairix.quality.eval.cli import main as legacy_main

    legacy_main(argv_list)
    # Legacy CLI exits via sys.exit; if it returns we treat that as 0.
    return 0


def resolve_deps(
    *,
    paths: KairixPaths | None,
    fact_store: FactStore | None,
    fact_extractor: FactExtractor | None,
    llm: LLMBackend | None,
    search_pipeline: SearchPipeline | None,
    document_writer: DocumentWriter | None,
    embedder: CorpusEmbedder | None,
    consolidation: ConsolidationPass | None,
    via_prep: bool,
    err_sink: TextIO,
) -> _ResolvedDeps | int:
    """Resolve every collaborator the suite path needs, or return an exit code."""
    resolved_paths = paths if paths is not None else KairixPaths.resolve()

    if fact_store is None:
        try:
            fact_store = resolve_production_fact_store(resolved_paths.db_path)
        except ImportError as exc:
            err_sink.write(f"{_ERROR_PREFIX}{exc}\n")
            return 2

    if llm is None:
        try:
            llm = resolve_production_llm()
        except ImportError as exc:
            err_sink.write(f"{_ERROR_PREFIX}{exc}\n")
            return 2

    # Production-default FactExtractor — the LoCoMo verification gap fix.
    # Before this wiring, ``kairix eval`` defaulted to ``_NullFactExtractor``
    # which returns ``[]`` regardless of input, so the suite runner
    # extracted 0/N facts on every conversational corpus. The composition
    # root in :mod:`kairix.corpus.wiring` builds the real
    # :class:`LLMFactExtractor` wired to the resolved LLM backend.
    resolved_extractor: FactExtractor = (
        fact_extractor if fact_extractor is not None else resolve_production_fact_extractor(llm, err_sink=err_sink)
    )

    resolved_pipeline = resolve_search_pipeline(
        override=search_pipeline,
        via_prep=via_prep,
        err_sink=err_sink,
    )
    if isinstance(resolved_pipeline, int):
        return resolved_pipeline

    # Spike C1 Phase 3 — DocumentWriter / CorpusEmbedder /
    # ConsolidationPass are passed through unchanged. Production
    # defaults are deferred until ``kairix.corpus.wiring`` lands its
    # factory functions; callers (tests + future opt-in operators)
    # inject explicit implementations via the ``main()`` kwargs.
    return _ResolvedDeps(
        paths=resolved_paths,
        fact_store=fact_store,
        fact_extractor=resolved_extractor,
        llm=llm,
        search_pipeline=resolved_pipeline,
        document_writer=document_writer,
        embedder=embedder,
        consolidation=consolidation,
    )


def import_search_pipeline_builder() -> Callable[[], SearchPipeline]:
    """Import :func:`kairix.core.factory.build_search_pipeline`.

    Extracted so the import is a single seam tests can swap by passing
    a ``builder_loader`` to :func:`resolve_search_pipeline`. Raises
    :class:`ImportError` straight through; the caller handles the
    operator-visible warning.
    """
    from kairix.core.factory import build_search_pipeline

    builder: Callable[[], SearchPipeline] = build_search_pipeline
    return builder


def resolve_search_pipeline(
    *,
    override: SearchPipeline | None,
    via_prep: bool,
    err_sink: TextIO,
    builder_loader: Callable[[], Callable[[], SearchPipeline]] | None = None,
) -> SearchPipeline | int | None:
    """Resolve the SearchPipeline given the CLI mode + caller-supplied override.

    Priority:

    1. Caller-supplied ``override`` always wins (tests inject fakes via
       this kwarg; the CLI flag never overrides an explicit override).
    2. ``--legacy-direct`` (i.e. ``via_prep=False``) returns ``None`` so
       the SuiteRunner falls back to ``fact_store.search``.
    3. Default (``via_prep=True``) constructs the production pipeline
       via :func:`build_search_pipeline`. Returns exit code 2 with an
       actionable error on ImportError.

    ``builder_loader`` is the documented composition seam — tests inject
    a raising loader to drive the ImportError branch. NOT test-only
    (F6 clean): same swap-point shape as ``resolve_production_fact_extractor``.
    """
    if override is not None:
        return override
    if not via_prep:
        return None
    loader = builder_loader if builder_loader is not None else import_search_pipeline_builder
    try:
        builder = loader()
    except ImportError as exc:
        err_sink.write(
            f"{_ERROR_PREFIX}cannot import build_search_pipeline — {exc}. "
            f"fix: ensure your kairix install includes kairix.core.factory. "
            f"next: re-run with --legacy-direct to bypass the pipeline.\n"
        )
        return 2
    return builder()


def _execute_suite(
    *,
    args: argparse.Namespace,
    suite_path: Path,
    deps: _ResolvedDeps,
    out_sink: TextIO,
    err_sink: TextIO,
) -> int:
    """Run the suite via :class:`SuiteRunner` and emit the result."""
    runner = SuiteRunner(
        fact_store=deps.fact_store,
        fact_extractor=deps.fact_extractor,
        llm=deps.llm,
        paths=deps.paths,
        search_pipeline=deps.search_pipeline,
        document_writer=deps.document_writer,
        embedder=deps.embedder,
        consolidation=deps.consolidation,
    )

    try:
        suite = runner.discover_suite(suite_path)
    except ValueError as exc:
        err_sink.write(f"{_ERROR_PREFIX}{exc}\n")
        return 2

    result = runner.run(suite)
    _emit_result(result=result, suite_path=suite_path, as_json=args.as_json, out_sink=out_sink)

    if args.regression_against:
        return _check_regression(
            result=result,
            baseline_dir=Path(args.regression_against),
            err_sink=err_sink,
        )
    return 0


def _emit_result(*, result: SuiteResult, suite_path: Path, as_json: bool, out_sink: TextIO) -> None:
    """Write the SuiteResult to ``out_sink`` in the requested shape."""
    if as_json:
        out_sink.write(json.dumps(dataclasses.asdict(result), indent=2, default=str) + "\n")
    else:
        out_sink.write(_format_human(result, suite_path=suite_path))


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    """Argparse for the Plan B-parity ``kairix eval <suite_path>`` shape."""
    parser = argparse.ArgumentParser(
        prog="kairix eval",
        description=(
            "Run a conversation eval suite (sessions + ground-truth queries) "
            "against the configured backend and report per-category scores."
        ),
    )
    parser.add_argument(
        "suite_path",
        help="Path to the suite directory (e.g. reference-library/conversations/team-alpha).",
    )
    parser.add_argument(
        "--metric",
        choices=("query-pass-rate", "extractor-f1", "both"),
        default="both",
        help="Which metric(s) to report (default: both).",
    )
    parser.add_argument(
        "--backend",
        choices=_BACKENDS,
        default="kairix-native",
        help=f"Memory backend to evaluate (default: kairix-native). One of {_BACKENDS}.",
    )
    parser.add_argument(
        "--regression-against",
        default=None,
        help="Path to a pinned baseline directory; exit 1 if the run regresses by more than 2pp.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the SuiteResult as JSON instead of human-readable text.",
    )
    # Plan B-parity D3 — eval-vs-prep convergence. ``--via-prep`` is the
    # new default: score every question through the same SearchPipeline
    # ``kairix prep`` uses, so eval scores reflect the operator-visible
    # path. ``--legacy-direct`` keeps the pre-D3 ``fact_store.search``
    # behaviour around as a regression-debugging escape hatch — slated
    # for removal in v2026.5.19.
    pipeline_group = parser.add_mutually_exclusive_group()
    pipeline_group.add_argument(
        "--via-prep",
        action="store_true",
        dest="via_prep",
        default=True,
        help=(
            "Route every question through the same SearchPipeline kairix prep uses "
            "(default; intent classifier + fact federation + fusion + L0 synthesis). "
            "Plan B-parity D3 convergence — eval scores now match the operator-visible "
            "kairix prep path."
        ),
    )
    pipeline_group.add_argument(
        "--legacy-direct",
        action="store_false",
        dest="via_prep",
        help=(
            "Bypass the SearchPipeline; score against fact_store.search hits directly. "
            "For regression debugging only — slated for removal in v2026.5.19."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_human(result: SuiteResult, *, suite_path: Path) -> str:
    """Render the human-readable summary documented in the brief."""
    overall_pct = pct(result.n_passed, result.n_questions)
    lines: list[str] = [
        f"Suite: {result.suite_name} (path={suite_path})",
        f"  Questions  : {result.n_passed}/{result.n_questions} ({overall_pct}%)",
        f"  Mean score : {result.mean_score:.3f}",
        "  By category:",
    ]
    for cat, stats in sorted(result.per_category.items()):
        n = int(stats["n"])
        passed = int(stats["passed"])
        mean = stats["mean"]
        cat_pct = pct(passed, n)
        lines.append(f"    {cat:<14} {passed}/{n} ({cat_pct}%) mean={mean:.3f}")
    if result.per_extraction_f1 is not None:
        lines.append(
            f"  Extractor F1: {result.per_extraction_f1:.2f} "
            f"(precision {result.extraction_precision:.2f}, "
            f"recall {result.extraction_recall:.2f})"
        )
    return "\n".join(lines) + "\n"


def pct(passed: int, total: int) -> int:
    """Integer percentage; 0 when total is zero (avoids divide-by-zero)."""
    if total <= 0:
        return 0
    return round(100 * passed / total)


# ---------------------------------------------------------------------------
# Regression gate
# ---------------------------------------------------------------------------


def _check_regression(
    *,
    result: SuiteResult,
    baseline_dir: Path,
    err_sink: TextIO,
) -> int:
    """Compare ``result`` against the pinned baseline; return CLI exit code.

    Baseline file: ``baseline_dir/<suite_name>.json`` carrying a
    previously-serialised :class:`SuiteResult`. Regression =
    ``baseline.mean_score - result.mean_score > 2pp`` (0.02 on the
    0.0-1.0 scale).
    """
    baseline_path = baseline_dir / f"{result.suite_name}.json"
    if not baseline_path.exists():
        err_sink.write(
            f"{_ERROR_PREFIX}baseline file {baseline_path!r} is missing. "
            f"fix: write the current result there to pin a baseline. "
            f"next: re-run with --regression-against pointed at the same dir.\n"
        )
        return 2

    try:
        baseline_raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err_sink.write(
            f"{_ERROR_PREFIX}baseline {baseline_path!r} is not valid JSON: {exc}. "
            f"fix: regenerate via `kairix eval <suite> --json > {baseline_path}`. "
            f"next: re-run the regression gate.\n"
        )
        return 2

    baseline_mean = float(baseline_raw.get("mean_score", 0.0))
    delta_pp = (baseline_mean - result.mean_score) * 100.0
    if delta_pp > _REGRESSION_TOLERANCE_PP:
        err_sink.write(
            f"{_ERROR_PREFIX}REGRESSION on {result.suite_name} — "
            f"baseline mean={baseline_mean:.3f} vs run mean={result.mean_score:.3f} "
            f"(delta={delta_pp:.2f}pp; tolerance={_REGRESSION_TOLERANCE_PP}pp). "
            f"fix: investigate the recall/extractor delta on this suite. "
            f"next: re-run after the fix, then update the baseline.\n"
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# Production-time fallbacks (kept narrow; tests inject fakes via kwargs).
# ---------------------------------------------------------------------------


def resolve_production_fact_store(db_path: Path) -> FactStore:
    """Return a production FactStore or raise ImportError with actionable hint.

    The ImportError rewrap is defensive: ``kairix.core.facts`` is a Cap
    #3 subpackage that's always present in any valid kairix install.
    The branch is unreachable through normal execution and is excluded
    from coverage on that basis (rather than driving it through a
    test-only injection seam on a private helper).
    """
    try:
        from kairix.core.facts import SQLiteFactStore
    except ImportError as exc:  # pragma: no cover — defensive: Cap #3 subpkg always present
        raise ImportError(
            "kairix eval needs SQLiteFactStore (Capability #3). "
            "fix: ensure your kairix install includes kairix.core.facts. "
            "next: re-run after installing the current build."
        ) from exc
    store: FactStore = SQLiteFactStore(db_path=db_path)
    return store


def import_production_extractor_factory() -> Callable[[LLMBackend], FactExtractor]:
    """Import :func:`kairix.corpus.wiring.make_production_fact_extractor`.

    Extracted so the import + lookup is a single atomic step the caller
    can wrap in one ``try/except``. Re-raised ImportError carries no
    extra message because the caller adds F21-shaped guidance.
    """
    from kairix.corpus.wiring import make_production_fact_extractor

    factory: Callable[[LLMBackend], FactExtractor] = make_production_fact_extractor
    return factory


def resolve_production_fact_extractor(
    llm: LLMBackend,
    *,
    err_sink: TextIO,
    factory_loader: Callable[[], Callable[[LLMBackend], FactExtractor]] | None = None,
) -> FactExtractor:
    """Return the production :class:`FactExtractor` or the Null fallback.

    Production path: resolve
    :func:`kairix.corpus.wiring.make_production_fact_extractor` and let
    it wire :class:`~kairix.core.facts.extractor.LLMFactExtractor`
    against ``llm``. Fallback path: when the import or factory call
    raises, emit an F21-shaped warning on ``err_sink`` and return
    :class:`_NullFactExtractor` so the suite runner still completes —
    just with zero facts (today's regression-debugging behaviour, but
    now visible to the operator rather than silent).

    Why a fallback at all? The Plan B-parity post-mortem (#208) cared
    about closing the SILENT degradation: 0 facts with no signal. With
    this helper, an operator who sees zero facts AND a stderr warning
    knows the wiring is broken; an operator who sees zero facts and no
    warning knows the wiring is fine and the corpus actually carries
    no extractable content.

    Parameters
    ----------
    llm:
        The resolved :class:`LLMBackend` to thread through the wiring.
    err_sink:
        Writable text sink — operator-visible warnings land here.
    factory_loader:
        Composition seam — when ``None``, production resolves via
        :func:`import_production_extractor_factory`. Tests inject a
        raising loader to drive the ImportError fallback OR a loader
        that returns a raising factory to drive the broad-except
        fallback. This kwarg is NOT test-only (F6 clean): it is the
        documented swap point between the ``resolve_deps`` orchestrator
        and the wiring layer, used by any future caller wanting to
        pin a non-default factory resolution strategy.
    """
    loader = factory_loader if factory_loader is not None else import_production_extractor_factory
    try:
        factory = loader()
    except ImportError as exc:
        err_sink.write(
            f"{_ERROR_PREFIX}cannot import kairix.corpus.wiring — {exc}. "
            f"fix: ensure your kairix install includes kairix.corpus.wiring. "
            f"next: re-run after installing the current build. "
            f"run: pip install -e . from the repo root.\n"
        )
        return _NullFactExtractor()
    try:
        return factory(llm)
    except Exception as exc:  # Wiring failures degrade to Null + warning, never crash eval.
        err_sink.write(
            f"{_ERROR_PREFIX}make_production_fact_extractor raised — {exc}. "
            f"fix: check the LLM backend resolves correctly via "
            f"kairix.platform.llm.get_default_backend. "
            f"next: re-run with --legacy-direct to bypass the pipeline OR "
            f"inject a FactExtractor explicitly via the use_cases.eval_suite.main "
            f"kwarg. "
            f"run: kairix probe-config to verify provider wiring.\n"
        )
        return _NullFactExtractor()


def resolve_production_llm() -> LLMBackend:
    """Return the configured production LLM backend.

    Resolves via :func:`kairix.platform.llm.get_default_backend` so the
    Plan B-parity surface honours the operator's provider config the
    same way every other production-time LLM call does.

    The ImportError rewrap is defensive: ``kairix.platform.llm`` ships
    with every kairix install. The branch is unreachable through normal
    execution and is excluded from coverage on that basis (rather than
    driving it through a test-only injection seam on a private helper).
    """
    try:
        from kairix.platform.llm import get_default_backend
    except ImportError as exc:  # pragma: no cover — defensive: subpkg always present
        raise ImportError(
            "kairix eval cannot resolve the configured LLM backend. "
            "fix: check the kairix.platform.llm module is present. "
            "next: re-run after fixing the install."
        ) from exc
    backend: LLMBackend = get_default_backend()
    return backend


class _NullFactExtractor:
    """Production placeholder extractor — emits zero facts.

    Capability #2 (sister agent) lands the LLM-driven extractor. Until
    then, the production-wired suite runs against an empty extraction
    set so the operator can still exercise the recall + judge path
    against the document corpus.
    """

    def extract(
        self,
        *,
        turns: list[dict[str, Any]],
        window_hint: dict[str, Any] | None = None,
        session_metadata: dict[str, Any] | None = None,
    ) -> list[Any]:
        """Return ``[]`` — production placeholder, tests inject FakeFactExtractor."""
        # Reference the parameter names so F19 sees a Load-context use of
        # ``turns`` / ``window_hint`` / ``session_metadata`` (the names are
        # mandated by the FactExtractor Protocol — renaming with ``_``
        # prefix would break the runtime contract).
        _ = (turns, window_hint, session_metadata)
        return []


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
