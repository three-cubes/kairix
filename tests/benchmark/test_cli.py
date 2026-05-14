"""Unit tests for ``kairix.quality.benchmark.cli`` (resolves #222 UX).

Cover the dispatch surface that the issue cares about:
- ``resolve_collection`` honours explicit operator input over suite metadata.
- ``cmd_run`` auto-scopes to ``suite.meta.default_collection`` when
  ``--collection`` is not passed.
- ``cmd_run`` lets an explicit ``--collection`` override
  ``default_collection``.
- ``cmd_list`` emits the bundled-suite metadata expected by users.
- An unknown suite name exits non-zero with a "did you mean: list" hint.

All collaborators flow through ``BenchmarkCLIDeps`` — no ``@patch`` (F1),
no env-var monkeypatching (F2), no internal-name imports (F5).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.cli import (
    BenchmarkCLIDeps,
    cmd_compare,
    cmd_init,
    cmd_list,
    cmd_run,
    cmd_validate,
    main,
    resolve_collection,
)

# ---------------------------------------------------------------------------
# Test helpers — capturing fakes used as ``BenchmarkCLIDeps`` collaborators.
# ---------------------------------------------------------------------------


class _CapturingRunner:
    """Fake ``run_benchmark`` that records its kwargs and returns a stub
    BenchmarkResult. No real retrieval — the CLI's job is wiring kwargs
    through, which this fake fully exercises."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        # Build the minimal shape ``format_interpretation`` walks: meta + summary.
        from kairix.quality.benchmark.runner import BenchmarkResult

        return BenchmarkResult(
            meta={"system": "fake", "agent": None, "date": "2026-05-14", "collection": kwargs.get("collection")},
            summary={
                "weighted_total": 0.5,
                "category_scores": {},
            },
            diagnostics={},
            cases=[],
        )


# ---------------------------------------------------------------------------
# Suite-by-name and unknown-suite handling
# ---------------------------------------------------------------------------


@pytest.fixture
def bundled_suites(tmp_path: Path) -> Path:
    """Minimal bundled-suites directory with a reflib suite carrying
    ``default_collection`` metadata. Tests pass the dir explicitly to
    ``resolve_suite_path`` so no env-var monkeypatch is needed (F2)."""
    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "reflib-gold-v3.yaml").write_text(
        "meta:\n"
        "  name: reflib\n"
        "  description: stub suite for cli tests\n"
        "  default_collection: reference-library\n"
        "cases:\n"
        "  - id: t1\n"
        "    category: recall\n"
        "    query: hello\n"
        "    score_method: exact\n"
        "    gold_title: hello\n",
    )
    return suites


def _make_run_args(suite: str, *, collection: str | None = None) -> Any:
    """Build the argparse.Namespace ``cmd_run`` expects without re-parsing argv.
    Keeps the test focused on the cmd_run logic, not argparse plumbing. The
    ``run_benchmark`` collaborator is injected as a fake so ``system="hybrid"``
    never touches the real retrieval stack."""
    import argparse

    return argparse.Namespace(
        subcommand="run",
        suite=suite,
        system="hybrid",
        agent=None,
        collection=collection,
        fusion=None,
        output=None,
    )


# ---------------------------------------------------------------------------
# resolve_collection — pure helper, no I/O.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_collection_explicit_wins_over_default() -> None:
    """Explicit operator-supplied collection must beat suite metadata."""
    col, auto = resolve_collection("user-pick", "reference-library")
    assert col == "user-pick"
    assert auto is False


@pytest.mark.unit
def test_resolve_collection_falls_back_to_default() -> None:
    """When explicit is None, suite default is applied and flagged auto-scoped."""
    col, auto = resolve_collection(None, "reference-library")
    assert col == "reference-library"
    assert auto is True


@pytest.mark.unit
def test_resolve_collection_no_default_no_explicit_returns_none() -> None:
    """Neither side provides a collection → no scoping, no auto-scope marker."""
    col, auto = resolve_collection(None, None)
    assert col is None
    assert auto is False


@pytest.mark.unit
def test_resolve_collection_empty_explicit_string_is_explicit() -> None:
    """Operator passing --collection '' is a deliberate signal (empty != None)."""
    col, auto = resolve_collection("", "reference-library")
    assert col == ""  # explicit empty string honoured
    assert auto is False


# ---------------------------------------------------------------------------
# cmd_run — auto-scoping and override behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_auto_scopes_to_default_collection(bundled_suites: Path) -> None:
    """When --collection is omitted, cmd_run reads default_collection from
    the suite YAML and threads it into the runner. This is the main UX fix
    from #222 — without this the bundled reflib suite scores 0.065 instead
    of 0.890."""
    runner = _CapturingRunner()
    args = _make_run_args(str(bundled_suites / "reflib-gold-v3.yaml"))

    rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0
    assert len(runner.calls) == 1
    assert runner.calls[0]["collection"] == "reference-library"


@pytest.mark.unit
def test_cmd_run_explicit_collection_overrides_default(bundled_suites: Path) -> None:
    """--collection user-pick must override suite.meta.default_collection."""
    runner = _CapturingRunner()
    args = _make_run_args(
        str(bundled_suites / "reflib-gold-v3.yaml"),
        collection="user-pick",
    )

    rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0
    assert runner.calls[0]["collection"] == "user-pick"


@pytest.mark.unit
def test_cmd_run_prints_auto_scope_notice(bundled_suites: Path) -> None:
    """Auto-scoping is logged so operators see why a collection was chosen."""
    runner = _CapturingRunner()
    args = _make_run_args(str(bundled_suites / "reflib-gold-v3.yaml"))

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))
    assert "auto-scoping to collection 'reference-library'" in out.getvalue()


@pytest.mark.unit
def test_cmd_run_no_auto_scope_notice_when_explicit(bundled_suites: Path) -> None:
    """Auto-scope notice must NOT fire when --collection is explicit."""
    runner = _CapturingRunner()
    args = _make_run_args(
        str(bundled_suites / "reflib-gold-v3.yaml"),
        collection="user-pick",
    )

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))
    assert "auto-scoping" not in out.getvalue()


@pytest.mark.unit
def test_cmd_run_unknown_suite_exits_one_with_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown suite name must exit 1 and point operators at
    ``kairix benchmark list``. We ``chdir`` into an empty tmp dir so
    ``resolve_suite_path``'s fallback to ``kairix.paths.bundled_suites_root()``
    (which defaults to the relative ``"suites"`` path) finds nothing — no
    KAIRIX_* env-var monkeypatching needed (F2-clean)."""
    monkeypatch.chdir(tmp_path)
    args = _make_run_args("does-not-exist")  # bare bundle name, no slashes

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cmd_run(args)
    assert rc == 1
    assert "did you mean" in err.getvalue()
    assert "kairix benchmark list" in err.getvalue()


@pytest.mark.unit
def test_cmd_run_passes_system_and_agent_through(bundled_suites: Path) -> None:
    """All operator-supplied flags must reach the runner unchanged."""
    runner = _CapturingRunner()
    args = _make_run_args(str(bundled_suites / "reflib-gold-v3.yaml"))
    args.system = "bm25"
    args.agent = "builder"
    args.fusion = "rrf"
    args.output = "/tmp/out"

    cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    call = runner.calls[0]
    assert call["system"] == "bm25"
    assert call["agent"] == "builder"
    assert call["fusion_override"] == "rrf"
    assert call["output_dir"] == "/tmp/out"


# ---------------------------------------------------------------------------
# cmd_list — bundled-suite enumeration.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_list_prints_name_cases_default_collection() -> None:
    """The list output must include suite name, case count, and
    default_collection — the three fields the issue asks for."""
    import argparse

    fake_suites = [
        {
            "name": "reflib-gold-v3",
            "path": "/x/reflib-gold-v3.yaml",
            "default_collection": "reference-library",
            "n_cases": 200,
            "description": "Reference library retrieval gold suite",
        },
    ]
    deps = BenchmarkCLIDeps(list_suites=lambda: fake_suites)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_list(argparse.Namespace(subcommand="list"), deps=deps)
    text = out.getvalue()

    assert rc == 0
    assert "reflib-gold-v3" in text
    assert "200" in text
    assert "reference-library" in text
    assert "Reference library retrieval gold suite" in text


@pytest.mark.unit
def test_cmd_list_handles_missing_default_collection() -> None:
    """A suite without default_collection prints a dash placeholder, not None."""
    import argparse

    fake_suites = [
        {
            "name": "contract-suite",
            "path": "/x/contract-suite.yaml",
            "default_collection": None,
            "n_cases": 5,
            "description": None,
        },
    ]
    deps = BenchmarkCLIDeps(list_suites=lambda: fake_suites)

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_list(argparse.Namespace(subcommand="list"), deps=deps)
    text = out.getvalue()

    assert "contract-suite" in text
    assert "None" not in text  # placeholder used, not the str(None)
    assert "—" in text


@pytest.mark.unit
def test_cmd_list_empty_returns_one_with_hint() -> None:
    """No bundled suites → exit 1 and a hint to point KAIRIX_SUITES_ROOT."""
    import argparse

    deps = BenchmarkCLIDeps(list_suites=lambda: [])

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_list(argparse.Namespace(subcommand="list"), deps=deps)

    assert rc == 1
    assert "No bundled suites found" in out.getvalue()
    assert "KAIRIX_SUITES_ROOT" in out.getvalue()


# ---------------------------------------------------------------------------
# main() dispatch — verifies subcommand routing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_dispatches_list_to_cmd_list() -> None:
    """`kairix benchmark list` must invoke cmd_list with the supplied deps."""
    captured: list[bool] = []

    def _fake_list() -> list[dict]:
        captured.append(True)
        return []

    deps = BenchmarkCLIDeps(list_suites=_fake_list)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["list"], deps=deps)
    assert rc == 1  # empty list returns 1
    assert captured == [True]


@pytest.mark.unit
def test_main_dispatches_run_to_cmd_run(bundled_suites: Path) -> None:
    """`kairix benchmark run --suite <path>` routes to cmd_run with deps."""
    runner = _CapturingRunner()
    deps = BenchmarkCLIDeps(run_benchmark=runner)

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(
            [
                "run",
                "--suite",
                str(bundled_suites / "reflib-gold-v3.yaml"),
                "--system",
                "bm25",
            ],
            deps=deps,
        )
    assert rc == 0
    assert len(runner.calls) == 1
    # Default --collection is None, so default_collection from the suite YAML
    # must be auto-applied (the whole point of #222).
    assert runner.calls[0]["collection"] == "reference-library"


# ---------------------------------------------------------------------------
# cmd_compare — exercises file-loading and delta formatting.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_compare_missing_file_exits_one(tmp_path: Path) -> None:
    """A non-existent result path must exit 1 with an error message."""
    import argparse

    args = argparse.Namespace(
        subcommand="compare",
        result_a=str(tmp_path / "nope-a.json"),
        result_b=str(tmp_path / "nope-b.json"),
    )

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_compare(args)
    assert rc == 1
    assert "Error loading" in err.getvalue()


@pytest.mark.unit
def test_cmd_compare_prints_delta(tmp_path: Path) -> None:
    """Two valid result JSONs produce a delta line in the comparison output."""
    import argparse

    a = {
        "meta": {"system": "old", "date": "2026-01-01"},
        "summary": {
            "weighted_total": 0.5,
            "category_scores": {"recall": 0.4, "conceptual": 0.6},
            "ndcg_at_10": 0.45,
        },
    }
    b = {
        "meta": {"system": "new", "date": "2026-05-14"},
        "summary": {
            "weighted_total": 0.8,
            "category_scores": {"recall": 0.7, "conceptual": 0.9},
            "ndcg_at_10": 0.75,
        },
    }
    a_path = tmp_path / "a.json"
    b_path = tmp_path / "b.json"
    a_path.write_text(json.dumps(a))
    b_path.write_text(json.dumps(b))
    args = argparse.Namespace(
        subcommand="compare",
        result_a=str(a_path),
        result_b=str(b_path),
    )

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_compare(args)
    text = out.getvalue()

    assert rc == 0
    assert "BENCHMARK COMPARISON" in text
    assert "Delta" in text
    # B-A = 0.3, marker should be the up arrow
    assert "0.300" in text
    assert "▲" in text  # higher score in B
    # NDCG@10 row should also appear
    assert "NDCG@10" in text


# ---------------------------------------------------------------------------
# cmd_init — scaffolding behaviour.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_init_creates_scaffold(tmp_path: Path) -> None:
    """cmd_init writes a YAML file with the agent's name baked in."""
    import argparse

    out_path = tmp_path / "agent-alpha.yaml"
    args = argparse.Namespace(
        subcommand="init",
        agent="agent-alpha",
        collections=None,
        output=str(out_path),
    )

    captured = io.StringIO()
    with redirect_stdout(captured):
        rc = cmd_init(args)

    assert rc == 0
    assert out_path.exists()
    body = out_path.read_text()
    assert "agent-alpha" in body
    assert "recall" in body  # template includes a recall case


@pytest.mark.unit
def test_cmd_init_refuses_overwrite(tmp_path: Path) -> None:
    """cmd_init must not clobber an existing file — operators rerun the
    command after editing and would lose work if it overwrote."""
    import argparse

    existing = tmp_path / "agent-alpha.yaml"
    existing.write_text("hand-written-content")
    args = argparse.Namespace(
        subcommand="init",
        agent="agent-alpha",
        collections=None,
        output=str(existing),
    )

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_init(args)

    assert rc == 1
    assert existing.read_text() == "hand-written-content"
    assert "already exists" in err.getvalue()


# ---------------------------------------------------------------------------
# cmd_validate — schema-only path (no DB available).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_validate_unknown_suite_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``validate`` for a missing suite exits 1 — mirrors ``run`` behaviour
    but cmd_validate handles both ValueError (bad YAML) and FileNotFoundError."""
    import argparse

    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(subcommand="validate", suite="ghost-suite")

    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_validate(args)
    assert rc == 1
    assert "Load error" in err.getvalue()


@pytest.mark.unit
def test_cmd_validate_runs_schema_check(bundled_suites: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_validate succeeds for a well-formed suite YAML. We don't assert the
    exact return code because it depends on whether the test host has a kairix
    index present (the F4-clean ``get_db_path`` reads the deployed paths), but
    a well-formed suite always prints the suite header and never crashes."""
    import argparse

    monkeypatch.chdir(bundled_suites.parent)

    suite_path = bundled_suites / "reflib-gold-v3.yaml"
    args = argparse.Namespace(subcommand="validate", suite=str(suite_path))

    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = cmd_validate(args)
    assert rc in (0, 1)
    assert "Suite:" in out.getvalue()


# ---------------------------------------------------------------------------
# Default callable wiring — production seams import on demand.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_list_suites_returns_a_list() -> None:
    """``BenchmarkCLIDeps()`` default for ``list_suites`` must resolve to a
    callable that returns ``list[dict]`` — even if the production root has
    no suites under it on the test host."""
    deps = BenchmarkCLIDeps()
    out = deps.list_suites()
    assert isinstance(out, list)
    # Every entry must carry the keys cmd_list reads.
    for entry in out:
        assert {"name", "path", "default_collection", "n_cases", "description"} <= set(entry.keys())


@pytest.mark.unit
def test_default_run_benchmark_delegates_to_runner(tmp_path: Path) -> None:
    """Default deps' ``run_benchmark`` lazy-imports and delegates. We exercise
    the lazy-import path with a fake-backed BenchmarkDeps so no real retrieval
    runs — the goal is to cover the wrapper, not run a benchmark."""
    from kairix.quality.benchmark.runner import BenchmarkDeps
    from kairix.quality.benchmark.suite import BenchmarkCase, BenchmarkSuite

    suite = BenchmarkSuite(
        meta={"name": "t", "version": "1.0"},
        cases=[
            BenchmarkCase(
                id="r1",
                category="recall",
                query="q",
                gold_path="g.md",
                score_method="exact",
            )
        ],
    )

    def _fake_retrieve(**_: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return ([], [], {})

    deps = BenchmarkCLIDeps()
    result = deps.run_benchmark(suite=suite, deps=BenchmarkDeps(retrieve=_fake_retrieve))
    # Returning a BenchmarkResult means the lazy import + delegation worked.
    assert result is not None
    assert result.meta["system"] == "hybrid"


@pytest.mark.unit
def test_cmd_compare_negative_delta_shows_down_arrow(tmp_path: Path) -> None:
    """When B scores lower than A, the delta marker must be the down arrow.
    Covers the negative-delta branch of ``_direction_marker``."""
    import argparse

    a = {"meta": {}, "summary": {"weighted_total": 0.9, "category_scores": {"recall": 0.9}}}
    b = {"meta": {}, "summary": {"weighted_total": 0.4, "category_scores": {"recall": 0.3}}}
    a_path = tmp_path / "a.json"
    b_path = tmp_path / "b.json"
    a_path.write_text(json.dumps(a))
    b_path.write_text(json.dumps(b))

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_compare(
            argparse.Namespace(
                subcommand="compare",
                result_a=str(a_path),
                result_b=str(b_path),
            )
        )
    assert "▼" in out.getvalue()


@pytest.mark.unit
def test_cmd_compare_invalid_json(tmp_path: Path) -> None:
    """A result file with malformed JSON must surface the error and exit 1.
    Covers the JSONDecodeError branch in cmd_compare's error handler."""
    import argparse

    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"meta": {}, "summary": {"weighted_total": 0.1, "category_scores": {}}}))

    args = argparse.Namespace(
        subcommand="compare",
        result_a=str(a),
        result_b=str(bad),
    )
    err = io.StringIO()
    with redirect_stderr(err):
        rc = cmd_compare(args)
    assert rc == 1
    assert "Error loading" in err.getvalue()


@pytest.mark.unit
def test_main_validate_dispatch(bundled_suites: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`kairix benchmark validate` routes to cmd_validate via main()."""
    monkeypatch.chdir(bundled_suites.parent)

    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        rc = main(["validate", "--suite", str(bundled_suites / "reflib-gold-v3.yaml")])
    assert rc in (0, 1)


@pytest.mark.unit
def test_main_init_dispatch(tmp_path: Path) -> None:
    """`kairix benchmark init` routes to cmd_init via main()."""
    out_path = tmp_path / "ag.yaml"
    with redirect_stdout(io.StringIO()):
        rc = main(["init", "--agent", "ag", "--output", str(out_path)])
    assert rc == 0
    assert out_path.exists()


@pytest.mark.unit
def test_main_compare_dispatch(tmp_path: Path) -> None:
    """`kairix benchmark compare` routes to cmd_compare via main()."""
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"meta": {}, "summary": {"weighted_total": 0.5, "category_scores": {}}}))
    b.write_text(json.dumps({"meta": {}, "summary": {"weighted_total": 0.6, "category_scores": {}}}))
    with redirect_stdout(io.StringIO()):
        rc = main(["compare", str(a), str(b)])
    assert rc == 0
