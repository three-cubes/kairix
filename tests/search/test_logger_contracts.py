"""Contract probes for kairix.core.search.logger.JsonlSearchLogger.

One @pytest.mark.contract test per documented claim in the module
docstring and class docstrings of ``kairix/core/search/logger.py``.

Discipline:
  - No @patch, no monkeypatch.setattr against logger internals.
  - No inline _Stub/_Fake/_Mock — uses canonical fakes from tests/fakes.py
    (FakeSearchLogger) where a Protocol-compliant double is needed.
  - Every test names the Protocol it is probing (SearchLogger).
  - Every assertion is sabotage-proven (mutually exclusive against the
    documented contract).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pytest

from kairix.core.protocols import SearchLogger
from kairix.core.search.logger import JsonlSearchLogger, default_search_log_paths
from tests.fakes import FakeSearchLogger


def _read_lines(path: Path) -> list[dict[str, object]]:
    """Read a JSONL file into a list of dicts. Empty file -> empty list."""
    if not path.exists():
        return []
    out: list[dict[str, object]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            out.append(json.loads(raw))
    return out


# ---------------------------------------------------------------------------
# Protocol conformance — JsonlSearchLogger and FakeSearchLogger both satisfy
# the SearchLogger Protocol.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_jsonl_search_logger_satisfies_search_logger_protocol(tmp_path: Path) -> None:
    """JsonlSearchLogger satisfies the SearchLogger Protocol (isinstance)."""
    lg = JsonlSearchLogger(search_log_path=tmp_path / "s.jsonl")
    assert isinstance(lg, SearchLogger)


@pytest.mark.contract
def test_canonical_fake_search_logger_satisfies_protocol() -> None:
    """tests.fakes.FakeSearchLogger structurally satisfies SearchLogger.

    Pinning the canonical fake against the Protocol prevents drift between
    the in-memory fake and the production JSONL adapter.
    """
    fake = FakeSearchLogger()
    assert isinstance(fake, SearchLogger)


# ---------------------------------------------------------------------------
# Documented claim: log_search appends one JSON line per call.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_search_appends_exactly_one_line_per_call(tmp_path: Path) -> None:
    """Each log_search call appends exactly one JSONL line."""
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    lg.log_search({"query_hash": "h1"})
    lg.log_search({"query_hash": "h2"})
    lg.log_search({"query_hash": "h3"})

    rows = _read_lines(log_path)
    assert len(rows) == 3
    assert [r["query_hash"] for r in rows] == ["h1", "h2", "h3"]


# ---------------------------------------------------------------------------
# Documented claim: ``ts`` is augmented with ISO 8601 UTC if absent.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_search_augments_missing_ts_as_iso8601_utc(tmp_path: Path) -> None:
    """When event lacks ``ts``, logger adds an ISO 8601 UTC string."""
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    lg.log_search({"query_hash": "h"})

    rows = _read_lines(log_path)
    assert len(rows) == 1
    ts = rows[0]["ts"]
    assert isinstance(ts, str)
    # ISO 8601 must parse via fromisoformat and carry a UTC offset.
    parsed = datetime.fromisoformat(ts)
    assert parsed.tzinfo is not None, f"ts {ts!r} must be timezone-aware (UTC)"
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0.0


# ---------------------------------------------------------------------------
# Documented claim: existing ``ts`` is preserved (not overwritten).
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_search_preserves_caller_supplied_ts(tmp_path: Path) -> None:
    """When caller sets ``ts``, logger does not overwrite it."""
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    sentinel_ts = "2026-05-04T12:00:00+00:00"
    lg.log_search({"query_hash": "h", "ts": sentinel_ts})

    rows = _read_lines(log_path)
    assert rows[0]["ts"] == sentinel_ts


# ---------------------------------------------------------------------------
# Documented claim: log_query is a no-op when query_log_path is None.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_query_is_noop_when_query_log_path_none(tmp_path: Path) -> None:
    """log_query writes nothing — anywhere — when query_log_path is None."""
    search_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=search_path, query_log_path=None)

    lg.log_query({"query": "secret-payload", "query_hash": "h"})

    # Nothing should be written. In particular the search log must not have
    # been used as a fallback target — privacy-gating is non-negotiable.
    assert not search_path.exists(), "log_query must not fall through to search_log_path when query_log_path is None"


# ---------------------------------------------------------------------------
# Documented claim: log_query writes one line when query_log_path is set.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_query_writes_to_query_path_when_configured(tmp_path: Path) -> None:
    """log_query writes one JSONL line to the configured query path."""
    search_path = tmp_path / "search.jsonl"
    query_path = tmp_path / "query.jsonl"
    lg = JsonlSearchLogger(search_log_path=search_path, query_log_path=query_path)

    lg.log_query({"query": "what is kairix", "query_hash": "h"})

    rows = _read_lines(query_path)
    assert len(rows) == 1
    assert rows[0]["query"] == "what is kairix"
    # Search log must be untouched — the two paths are independent.
    assert not search_path.exists()


# ---------------------------------------------------------------------------
# Documented claim: parent directories are created on first call.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_parent_directories_created_on_first_write(tmp_path: Path) -> None:
    """First write creates missing parent directories rather than raising."""
    nested = tmp_path / "missing" / "subdir" / "logs"
    log_path = nested / "search.jsonl"
    assert not nested.exists()

    lg = JsonlSearchLogger(search_log_path=log_path)
    lg.log_search({"query_hash": "h"})

    assert nested.is_dir()
    assert log_path.is_file()


# ---------------------------------------------------------------------------
# Documented claim: write failures NEVER raise — they are caught and logged
# at WARNING level. Search must not break because logging broke.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_log_search_does_not_raise_when_path_is_a_directory(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pointing search_log_path at a directory raises IsADirectoryError; the
    logger must catch it and emit a single WARNING — not propagate."""
    # tmp_path is itself a directory; opening it for append fails.
    lg = JsonlSearchLogger(search_log_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="kairix.core.search.logger"):
        # Must not raise — that is the contract.
        lg.log_search({"query_hash": "h"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log record on write failure"
    assert any("JsonlSearchLogger" in r.getMessage() for r in warnings)


@pytest.mark.contract
def test_log_query_does_not_raise_when_path_is_a_directory(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Same contract for log_query: write failures never propagate."""
    search_path = tmp_path / "search.jsonl"
    # tmp_path itself is a directory — passing it as query_log_path forces a write
    # failure when log_query is called.
    lg = JsonlSearchLogger(search_log_path=search_path, query_log_path=tmp_path)

    with caplog.at_level(logging.WARNING, logger="kairix.core.search.logger"):
        lg.log_query({"query": "x"})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log record on log_query write failure"


@pytest.mark.contract
def test_log_search_does_not_raise_on_unserialisable_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-JSON-serialisable event values trigger TypeError inside json.dumps;
    the logger must catch and warn — not propagate."""
    log_path = tmp_path / "search.jsonl"
    lg = JsonlSearchLogger(search_log_path=log_path)

    # An object() instance is not JSON-serialisable.
    bad_event = {"query_hash": "h", "junk": object()}

    with caplog.at_level(logging.WARNING, logger="kairix.core.search.logger"):
        lg.log_search(bad_event)  # must not raise

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log record on unserialisable event"


# ---------------------------------------------------------------------------
# Documented claim: default_search_log_paths is pure path-computation —
# no env var reads, no file I/O.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_default_search_log_paths_is_pure(tmp_path: Path) -> None:
    """Helper computes paths from arguments; never touches the filesystem."""
    base = tmp_path / "definitely_does_not_exist" / "logs"
    assert not base.exists()

    search_p, query_p = default_search_log_paths(base=base)

    assert search_p == base / "search.jsonl"
    assert query_p == base / "query.jsonl"
    # Pure computation — must not have created the directory.
    assert not base.exists()


@pytest.mark.contract
def test_default_search_log_paths_defaults_to_docker_base() -> None:
    """When base is None, the helper returns /data/kairix/logs paths.

    This is the documented Docker-production default. Callers wanting the
    non-Docker default (~/.cache/kairix/logs) must pass it explicitly —
    confirming that the boundary decision lives in factory.py, not here.
    """
    search_p, query_p = default_search_log_paths(base=None)

    assert search_p == Path("/data/kairix/logs/search.jsonl")
    assert query_p == Path("/data/kairix/logs/query.jsonl")


# ---------------------------------------------------------------------------
# Documented claim (G4 — config at boundary): the constructor takes keyword
# arguments only and does not read environment variables.
# ---------------------------------------------------------------------------


@pytest.mark.contract
def test_constructor_does_not_read_env_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The logger ignores any env vars at construction time.

    The documented G4 contract says: 'the logger never reads environment
    variables or config files'. We poison every plausible KAIRIX_* path
    variable; the logger must continue writing to the explicit constructor
    arg.

    monkeypatch is used here only on os.environ — never on the logger
    module itself — which is consistent with the project's no-monkeypatch
    rule (the rule is about substituting impls, not isolating env).
    """
    monkeypatch.setenv("KAIRIX_LOG_DIR", "/should/not/be/read")
    monkeypatch.setenv("KAIRIX_SEARCH_LOG_PATH", "/should/not/be/read.jsonl")
    monkeypatch.setenv("KAIRIX_QUERY_LOG_PATH", "/should/not/be/read.jsonl")

    explicit_path = tmp_path / "explicit.jsonl"
    lg = JsonlSearchLogger(search_log_path=explicit_path)

    lg.log_search({"query_hash": "h"})

    # The log landed on the explicit path — the env vars were ignored.
    rows = _read_lines(explicit_path)
    assert len(rows) == 1
    # And nothing got written to any of the env-named paths.
    assert not Path("/should/not/be/read.jsonl").exists()
