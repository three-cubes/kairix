"""F30 outcome test — ``kairix timeline`` subprocess surface.

Wave 0 Group C paydown for ``kairix/core/temporal/cli.py``. The
timeline CLI is the date-aware retrieval surface — operators ask
"what happened in April 2026" and the CLI renders a banner +
result list to stdout. Both surfaces (CLI and MCP) share the
``run_timeline`` use case so the binary surface is the operator's
contract.

The CLI is pure formatting on top of ``run_timeline``; both the
primary backend (temporal-chunks index) and the search-pipeline
fallback degrade gracefully to "No results found." when the index
isn't populated. That makes the F2-clean outcome test feasible
without setting up a fact corpus: ``run_timeline`` swallows the
backend errors (warning-log + empty results) and the CLI prints
its banner regardless.

Boundary chain exercised (happy path):

  subprocess([kairix, timeline, '<query>', --since YYYY-MM-DD,
              --until YYYY-MM-DD, --limit N])
    → kairix/core/temporal/cli.py:main
    → run_timeline(query, since=..., until=..., limit=N)
    → TimelineResult(time_window={...}, results=[], ...)
    → format_header + format_results → stdout

Sabotage-proof anchor: removing the ``Window:   ...`` line from
``format_header`` makes the happy-path test fail on the window-line
assertion. Removing the ``error: ...`` print + sys.exit(1) from
``main`` makes the error-path test fail on the exit-code assertion.
Tested locally.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def test_timeline_cli_subprocess_renders_banner_with_window() -> None:
    """Drive ``kairix timeline`` with explicit ``--since/--until`` window;
    assert on the rendered banner + window line + limit on stdout.

    The primary temporal-chunks backend is empty in this CI environment
    and the search-pipeline fallback degrades to a warning-log + empty
    results. Both layers are wrapped in try/except inside
    ``run_timeline`` so the CLI returns 0 + renders the banner — the
    operator-visible contract.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "timeline",
            "what happened",
            "--since",
            "2026-04-01",
            "--until",
            "2026-04-30",
            "--limit",
            "5",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, (
        f"timeline exited {proc.returncode}\n--- stderr ---\n{proc.stderr}\n--- stdout ---\n{proc.stdout}"
    )
    assert "Query:" in proc.stdout, f"expected Query: line in stdout: {proc.stdout!r}"
    assert "what happened" in proc.stdout, f"query echo missing: {proc.stdout!r}"
    assert "Window:" in proc.stdout, f"Window: line missing: {proc.stdout!r}"
    assert "2026-04-01" in proc.stdout, f"since-date missing in window: {proc.stdout!r}"
    assert "2026-04-30" in proc.stdout, f"until-date missing in window: {proc.stdout!r}"
    assert "Limit:" in proc.stdout, f"Limit: line missing: {proc.stdout!r}"


def test_timeline_cli_subprocess_exits_non_zero_on_invalid_since_date() -> None:
    """A malformed ``--since`` value → exit 1 + parse-error on stderr.

    Closes the binary-surface error path the unit tests
    (``tests/temporal/test_cli.py``) cover only via SystemExit catching.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "timeline",
            "topic",
            "--since",
            "not-a-real-date",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1, f"expected exit 1 for invalid date; got {proc.returncode}, stderr={proc.stderr!r}"
    assert "not-a-real-date" in proc.stderr, f"stderr should name bad date value: {proc.stderr!r}"
    assert "invalid" in proc.stderr.lower(), f"stderr should mention 'invalid': {proc.stderr!r}"
