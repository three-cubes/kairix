"""
Wikilink injection CLI for kairix.

Usage:
  kairix wikilinks inject --changed            inject files modified since last run
  kairix wikilinks inject --path path/to.md    inject a single file
  kairix wikilinks inject --dry-run            show what would be injected, no writes
  kairix wikilinks audit                       broken links + unlinked mentions report
  kairix wikilinks status                      entity count, last run, files processed
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from kairix.knowledge.wikilinks.injector import (
    _LOG_PATH,
    MAX_FILE_SIZE,
    inject_file,
    should_inject,
)
from kairix.knowledge.wikilinks.resolver import get_entities
from kairix.paths import KairixPaths

# Timestamp file to track last run
_LAST_RUN_PATH = os.environ.get("KAIRIX_DATA_DIR", str(Path.home() / ".cache" / "kairix")) + "/wikilinks-last-run"


def main(argv: list[str] | None = None, *, paths: KairixPaths | None = None) -> None:
    """Entry point for `kairix wikilinks` subcommand.

    Constructs the runtime ``KairixPaths`` once at the boundary and passes
    it down to every command handler — the only place this CLI module
    calls ``KairixPaths.resolve()``. Subcommands receive ``paths`` as a
    parameter, so tests inject a ``FakePaths`` via the ``paths`` keyword
    instead of monkeypatching ``KAIRIX_*`` environment variables.
    """
    if argv is None:
        argv = sys.argv[2:]  # strip "kairix wikilinks"

    if not argv:
        print(__doc__)
        sys.exit(0)

    if paths is None:
        paths = KairixPaths.resolve()

    subcmd = argv[0]

    if subcmd in ("--help", "-h", "help"):
        print(__doc__)
        sys.exit(0)
    elif subcmd == "inject":
        _inject_cmd(argv[1:], paths=paths)
    elif subcmd == "audit":
        _audit_cmd(argv[1:], paths=paths)
    elif subcmd == "status":
        _status_cmd(argv[1:])
    else:
        print(f"Unknown wikilinks subcommand: {subcmd}\n{__doc__}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# inject
# ---------------------------------------------------------------------------


def _inject_cmd(argv: list[str], *, paths: KairixPaths) -> None:
    """Handle `kairix wikilinks inject` with flags."""
    dry_run = "--dry-run" in argv
    changed_only = "--changed" in argv
    single_path: str | None = None

    if "--path" in argv:
        idx = argv.index("--path")
        if idx + 1 >= len(argv):
            print("--path requires a file path argument", file=sys.stderr)
            sys.exit(1)
        single_path = argv[idx + 1]

    entities = get_entities()
    if not entities:
        print(
            "⚠️  No entities loaded — check Neo4j connection and bootstrap index.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loaded {len(entities)} entities.")
    if dry_run:
        print("Dry-run mode: no files will be modified.\n")

    if single_path:
        _inject_single(single_path, entities, dry_run, paths=paths)
    elif changed_only:
        _inject_changed(entities, dry_run, paths=paths)
    else:
        _inject_all(entities, dry_run, paths=paths)

    if not dry_run:
        _write_last_run()


def _inject_single(path: str, entities: list[Any], dry_run: bool, *, paths: KairixPaths) -> None:
    """Inject wikilinks into a single file."""
    if not should_inject(path, paths=paths):
        print(f"⚠️  {path} is not eligible for injection.")
        return
    injected = inject_file(path, entities, dry_run=dry_run, paths=paths)
    if injected:
        mode = "(dry-run)" if dry_run else ""
        print(f"  ✅ {path} {mode}")
        for name in injected:
            print(f"     + [[{name}]]")
    else:
        print(f"  — {path}: no new links")


def _inject_all(entities: list[Any], dry_run: bool, *, paths: KairixPaths) -> None:
    """Inject wikilinks into all eligible vault and workspace files."""
    files = _gather_eligible_files(paths)
    total_files = 0
    total_links = 0

    for path in files:
        injected = inject_file(path, entities, dry_run=dry_run, paths=paths)
        if injected:
            total_files += 1
            total_links += len(injected)
            mode = "(dry-run)" if dry_run else ""
            print(f"  ✅ {path} {mode}")
            for name in injected:
                print(f"     + [[{name}]]")

    print(f"\nDone. {total_files} files updated, {total_links} wikilinks injected.")


def _inject_changed(entities: list[Any], dry_run: bool, *, paths: KairixPaths) -> None:
    """Inject only files modified since last run."""
    last_run = _read_last_run()
    if last_run is None:
        print("No previous run found — processing all eligible files.")
        _inject_all(entities, dry_run, paths=paths)
        return

    cutoff = last_run
    files = _gather_eligible_files(paths)
    changed = []
    for path in files:
        try:
            mtime = Path(path).stat().st_mtime
            if mtime >= cutoff:
                changed.append(path)
        except OSError:
            continue

    if not changed:
        print(f"No files modified since last run ({_fmt_ts(cutoff)}). Nothing to do.")
        return

    print(f"Processing {len(changed)} files modified since {_fmt_ts(cutoff)}.\n")
    total_files = 0
    total_links = 0
    for path in changed:
        injected = inject_file(path, entities, dry_run=dry_run, paths=paths)
        if injected:
            total_files += 1
            total_links += len(injected)
            mode = "(dry-run)" if dry_run else ""
            print(f"  ✅ {path} {mode}")
            for name in injected:
                print(f"     + [[{name}]]")

    print(f"\nDone. {total_files} files updated, {total_links} wikilinks injected.")


def _gather_eligible_files(paths: KairixPaths) -> list[str]:
    """Collect all eligible .md files from vault and workspaces."""
    result: list[str] = []
    for root in [str(paths.document_root), str(paths.workspace_root)]:
        p = Path(root)
        if not p.exists():
            continue
        for md_file in p.rglob("*.md"):
            path_str = str(md_file)
            if should_inject(path_str, paths=paths):
                try:
                    if md_file.stat().st_size <= MAX_FILE_SIZE:
                        result.append(path_str)
                except OSError:
                    continue
    return result


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _audit_cmd(argv: list[str], *, paths: KairixPaths) -> None:
    """Handle `kairix wikilinks audit`."""
    from kairix.knowledge.wikilinks.audit import weekly_report

    entities = get_entities()
    report = weekly_report(str(paths.document_root), entities, paths=paths)
    print(report)

    # Optionally save report to vault
    report_path = paths.document_root / "04-Agent-Knowledge" / "shared" / "wikilink-audit-report.md"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"\n📄 Report saved to {report_path}")
    except OSError as e:
        print(f"\n⚠️  Could not save report: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _status_cmd(argv: list[str]) -> None:
    """Handle `kairix wikilinks status`."""
    entities = get_entities()
    last_run = _read_last_run()
    log_entries = _read_log_entries()

    print("🔗 kairix Wikilinks Status")
    print("─" * 40)
    print(f"Entities loaded:    {len(entities)}")
    print(f"Last run:           {_fmt_ts(last_run) if last_run else 'never'}")

    if log_entries:
        total_files = len(log_entries)
        total_links = sum(len(e.get("injected", [])) for e in log_entries)
        real = sum(1 for e in log_entries if not e.get("dry_run"))
        dry = sum(1 for e in log_entries if e.get("dry_run"))
        print(f"Total log entries:  {total_files}")
        print(f"  Real injections:  {real}")
        print(f"  Dry runs:         {dry}")
        print(f"  Total links added: {total_links}")
    else:
        print("Injection log:      empty")

    print()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_last_run() -> None:
    """Write current timestamp to last-run marker file."""
    try:
        Path(_LAST_RUN_PATH).parent.mkdir(parents=True, exist_ok=True)
        Path(_LAST_RUN_PATH).write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _read_last_run() -> float | None:
    """Read timestamp from last-run marker file. Returns None if not found."""
    try:
        text = Path(_LAST_RUN_PATH).read_text(encoding="utf-8").strip()
        return float(text)
    except (OSError, ValueError):
        return None


def _read_log_entries() -> list[dict[str, Any]]:
    """Read all entries from injection log."""
    entries: list[dict[str, Any]] = []
    try:
        with open(_LOG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    except OSError:
        pass
    return entries


def _fmt_ts(ts: float | None) -> str:
    """Format a Unix timestamp as a human-readable string."""
    if ts is None:
        return "never"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
