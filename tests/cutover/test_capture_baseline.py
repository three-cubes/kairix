"""Unit tests for ``scripts/cutover/capture_baseline.py``.

The script is operator-facing release-ops tooling for the feature-flag
cutover protocol (see ``docs/architecture/feature-flag-architecture.md``
§4.2). These tests cover the pure-Python pieces — surface parsing,
SQLite state capture, envelope assembly, and graceful degradation when
inputs are missing — without invoking any external CLI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.cutover.capture_baseline import (
    ALL_SURFACES,
    _build_baseline,
    _capture_state_from_sqlite,
    _extract_latency_percentiles,
    _parse_surfaces,
    _run_sample_query,
    _write_envelope,
    main,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_documents_db(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """Create a minimal documents/content SQLite DB.

    ``rows`` is a list of (collection, hash, content) tuples — the script
    expects ``documents`` joined to ``content`` by hash.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT,
                hash TEXT,
                active INTEGER DEFAULT 1
            );
            """
        )
        for collection, h, doc in rows:
            conn.execute("INSERT OR IGNORE INTO content(hash, doc) VALUES (?, ?)", (h, doc))
            conn.execute(
                "INSERT INTO documents(collection, hash, active) VALUES (?, ?, 1)",
                (collection, h),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# _parse_surfaces
# ---------------------------------------------------------------------------


def test_parse_surfaces_all_expands_to_every_surface() -> None:
    """``--surface all`` expands to the canonical 4-item list."""
    assert _parse_surfaces("all") == list(ALL_SURFACES)


def test_parse_surfaces_csv_picks_listed_items() -> None:
    """CSV input keeps order + only the listed surfaces."""
    assert _parse_surfaces("state,latency") == ["state", "latency"]


def test_parse_surfaces_rejects_unknown_surface() -> None:
    """Unknown surface names raise ValueError with the action marker."""
    with pytest.raises(ValueError, match="fix:"):
        _parse_surfaces("state,bogus")


# ---------------------------------------------------------------------------
# _capture_state_from_sqlite
# ---------------------------------------------------------------------------


def test_capture_state_per_collection_groups_correctly(tmp_path: Path) -> None:
    """Per-collection roll-up matches the SQL contract in the spec."""
    db = tmp_path / "docs.db"
    _make_documents_db(
        db,
        [
            ("vault", "h1", "alpha"),
            ("vault", "h2", "beta-text"),
            ("crm", "h3", "g"),
        ],
    )
    state = _capture_state_from_sqlite(db)
    assert state is not None
    by_collection = {row["collection"]: row for row in state["per_collection"]}
    assert by_collection["vault"]["doc_count"] == 2
    assert by_collection["vault"]["total_bytes"] == len("alpha") + len("beta-text")
    assert by_collection["crm"]["doc_count"] == 1
    assert state["content_hash_digest"].startswith("sha256:")


def test_capture_state_missing_db_returns_none(tmp_path: Path) -> None:
    """Missing DB path -> None (caller marks the surface null + warns)."""
    assert _capture_state_from_sqlite(tmp_path / "does-not-exist.db") is None


def test_capture_state_skips_inactive_rows(tmp_path: Path) -> None:
    """Inactive documents are excluded from the roll-up."""
    db = tmp_path / "docs.db"
    _make_documents_db(db, [("vault", "h1", "alpha")])
    # Mark the row inactive
    conn = sqlite3.connect(db)
    conn.execute("UPDATE documents SET active = 0")
    conn.commit()
    conn.close()
    state = _capture_state_from_sqlite(db)
    assert state is not None
    assert state["per_collection"] == []


# ---------------------------------------------------------------------------
# _extract_latency_percentiles
# ---------------------------------------------------------------------------


def test_extract_latency_finds_top_level_percentiles() -> None:
    """Top-level p50/p95/p99 are recognised."""
    payload = {"p50_ms": 10.0, "p95_ms": 30.0, "p99_ms": 60.0}
    assert _extract_latency_percentiles(payload) == payload


def test_extract_latency_finds_nested_percentiles() -> None:
    """Nested ``latency`` subobject is also accepted."""
    payload = {"latency": {"p50": 12, "p95": 28, "p99": 55}}
    out = _extract_latency_percentiles(payload)
    assert out == {"p50_ms": 12.0, "p95_ms": 28.0, "p99_ms": 55.0}


def test_extract_latency_returns_none_when_incomplete() -> None:
    """A payload missing any percentile returns None."""
    assert _extract_latency_percentiles({"p50_ms": 10.0}) is None


# ---------------------------------------------------------------------------
# _build_baseline — assembled envelope shape
# ---------------------------------------------------------------------------


def test_build_baseline_missing_config_returns_null_surfaces(tmp_path: Path) -> None:
    """A missing config file yields nulls for state/eval/latency and the
    canned-fallback row for sample_journey.

    Post-#377 the sample-journey surface always emits at least one row —
    the operator-config fallback to ``DEFAULT_SAMPLE_QUERY`` — so the
    diff tool can compute parity against the same canned query on both
    sides. ``top_paths`` is empty in a sandbox where ``kairix`` isn't on
    PATH, but the row itself is present.
    """
    envelope = _build_baseline(
        flag="connector_dex_crm",
        config_path=tmp_path / "missing.yaml",
        surfaces=list(ALL_SURFACES),
    )
    assert envelope["flag"] == "connector_dex_crm"
    assert envelope["state"] is None
    # The sample-journey surface ships the canned-fallback row when the
    # config block is absent. top_paths is empty in a sandbox where the
    # kairix binary isn't reachable.
    assert envelope["sample_journey"] is not None
    assert len(envelope["sample_journey"]) == 1
    assert envelope["sample_journey"][0]["query"] == "default sample query"
    # eval / latency call out to subprocess — they're None when CLI unavailable
    # in a sandbox; in CI the kairix CLI exists but pointing at a fresh test
    # state still produces None (no documents indexed). Either way the
    # envelope shape is stable.
    assert "captured_at" in envelope
    assert "version" in envelope


def test_build_baseline_state_only_surface(tmp_path: Path) -> None:
    """``--surface state`` produces an envelope with only the state surface."""
    db = tmp_path / "docs.db"
    _make_documents_db(db, [("vault", "h1", "alpha")])
    config_path = tmp_path / "kairix.config.yaml"
    config_path.write_text(f"paths:\n  documents_db: {db}\n", encoding="utf-8")
    envelope = _build_baseline(
        flag="some_flag",
        config_path=config_path,
        surfaces=["state"],
    )
    assert envelope["flag"] == "some_flag"
    assert envelope["state"] is not None
    assert envelope["state"]["per_collection"][0]["collection"] == "vault"
    # Other surfaces were not requested -> absent from envelope
    assert "eval" not in envelope
    assert "latency" not in envelope
    assert "sample_journey" not in envelope


# ---------------------------------------------------------------------------
# _write_envelope + main — output path created with expected JSON keys
# ---------------------------------------------------------------------------


def test_write_envelope_creates_parents_and_emits_json(tmp_path: Path) -> None:
    """The output path's parent dir is created on demand."""
    out = tmp_path / "nested" / "deep" / "baseline.json"
    _write_envelope({"flag": "f", "state": None}, out)
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert loaded["flag"] == "f"
    assert loaded["state"] is None


def test_main_writes_expected_keys_to_out_path(tmp_path: Path) -> None:
    """End-to-end: main(argv) writes a JSON with the frozen top-level keys."""
    out = tmp_path / "baseline.json"
    rc = main(
        [
            "--flag",
            "test_flag",
            "--out",
            str(out),
            "--config",
            str(tmp_path / "missing.yaml"),
            "--surface",
            "state",
        ]
    )
    assert rc == 0
    loaded = json.loads(out.read_text())
    assert loaded["flag"] == "test_flag"
    assert "captured_at" in loaded
    assert "version" in loaded
    assert "state" in loaded


def test_main_rejects_unknown_surface(tmp_path: Path) -> None:
    """Bad --surface value exits 2 without writing the file."""
    out = tmp_path / "baseline.json"
    rc = main(
        [
            "--flag",
            "test_flag",
            "--out",
            str(out),
            "--config",
            str(tmp_path / "missing.yaml"),
            "--surface",
            "bogus",
        ]
    )
    assert rc == 2
    assert not out.exists()


# ---------------------------------------------------------------------------
# _run_sample_query — top-5 paths cap + tolerant path-key matching
# ---------------------------------------------------------------------------


def test_run_sample_query_returns_empty_when_runner_returns_none() -> None:
    """When the injected runner returns None, the query gets an empty list."""
    assert _run_sample_query("anything", runner=lambda _argv: None) == []


def test_run_sample_query_caps_at_top_5() -> None:
    """Even if the runner returns 10 hits, only top-5 paths are captured."""
    payload = {"results": [{"path": f"doc-{i}.md"} for i in range(10)]}
    paths = _run_sample_query("anything", runner=lambda _argv: payload)
    assert paths == [f"doc-{i}.md" for i in range(5)]


def test_run_sample_query_accepts_source_and_doc_path_keys() -> None:
    """Tolerates ``source`` / ``doc_path`` as path-like keys, not just ``path``."""
    payload = {
        "results": [
            {"path": "first.md"},
            {"source": "second.md"},
            {"doc_path": "third.md"},
        ]
    }
    paths = _run_sample_query("anything", runner=lambda _argv: payload)
    assert paths == ["first.md", "second.md", "third.md"]


def test_capture_marks_partial_benchmark_suite_instead_of_recording_scores(tmp_path: Path) -> None:
    """A suite whose benchmark report has judge failures is captured as a
    ``partial`` marker — its scores are never recorded as a baseline value.

    Sabotage-proof: drop the ``judge_failures(report)`` check in
    ``_capture_one_benchmark_suite`` — the partial run's scores are projected
    into the payload. Restored.
    """
    import subprocess as _subprocess

    from scripts.cutover.capture_baseline import _capture_one_benchmark_suite

    def _runner(argv: list[str], **_kwargs: object) -> _subprocess.CompletedProcess[str]:
        out_dir = Path(argv[argv.index("--output") + 1])
        report = {"summary": {"ndcg_at_10": 0.95, "weighted_total": 0.95, "judge_failures": 2}}
        (out_dir / "B-reflib-hybrid-2026-01-01.json").write_text(json.dumps(report), encoding="utf-8")
        return _subprocess.CompletedProcess(argv, 0, "", "")

    payload = _capture_one_benchmark_suite("reflib", _runner)
    assert payload == {"partial": True, "judge_failures": 2}
