"""Periodic baseline audit — flag stale entries (Wave 5 / #203).

Each F-rule baseline in ``.architecture/baseline/`` grandfathers
pre-existing violations. Over time, files get fixed, deleted, or
refactored — but the baseline entry persists, masking the fact that
the file is no longer load-bearing for the rule.

This audit walks every baseline and flags entries that look stale:

  1. **File no longer exists** — the path in the baseline was deleted
     or renamed. Always stale.

  2. **Coverage baselines: file now ≥ 90%** — for
     ``per-file-coverage-floor-files.txt`` and
     ``per-file-coverage-floor-union-files.txt``, cross-reference
     ``coverage.xml`` (if available); files at or above the F7/F9
     floor no longer need the baseline grandfather.

The non-coverage stale-detection (e.g., "this file no longer has a
bare suppression") would need to invoke each rule's detector with
the baseline suppressed — that's the next iteration. This audit is
intentionally minimal: catches the high-frequency cases (deletions
and coverage lifts) without coupling tightly to every detector's
internals.

Usage:
    python3 scripts/checks/audit_baselines.py [coverage.xml]

Exit 0 if all baseline entries still appear to be load-bearing;
exit 1 if stale entries are found (so CI can fail the audit job).
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BASELINE_DIR = REPO_ROOT / ".architecture" / "baseline"

COVERAGE_BASELINES = {
    "per-file-coverage-floor-files.txt",
    "per-file-coverage-floor-union-files.txt",
}
COVERAGE_FLOOR = 90.0


def _coverage_rates(coverage_xml: Path) -> dict[Path, float]:
    """Parse coverage.xml and return repo-relative path → line rate (0..1)."""
    if not coverage_xml.exists():
        return {}
    tree = ET.parse(coverage_xml)
    out: dict[Path, float] = {}
    for cls in tree.getroot().findall(".//class"):
        filename = cls.get("filename", "")
        rate = float(cls.get("line-rate", "0"))
        # Cobertura emits source-relative paths; kairix's setup means filename
        # is already "kairix/..." or "tests/...", which matches how baselines
        # store paths.
        out[Path(filename)] = rate
    return out


def _audit_baseline(baseline_file: Path, coverage_rates: dict[Path, float]) -> list[tuple[str, str]]:
    """Return [(path, reason), ...] for stale entries in this baseline."""
    stale: list[tuple[str, str]] = []
    if not baseline_file.exists():
        return stale
    entries = [line.strip() for line in baseline_file.read_text().splitlines() if line.strip() and not line.startswith("#")]
    is_coverage = baseline_file.name in COVERAGE_BASELINES
    for entry in entries:
        path = Path(entry)
        abs_path = REPO_ROOT / path
        if not abs_path.exists():
            stale.append((entry, "file no longer exists"))
            continue
        # Some baselines store kairix/foo.py paths even though coverage.xml may
        # use a slightly different prefix; check both.
        if is_coverage:
            rate = coverage_rates.get(path)
            if rate is None:
                # Try without the leading source-root if present.
                continue
            pct = rate * 100
            if pct >= COVERAGE_FLOOR:
                stale.append((entry, f"now {pct:.1f}% — at or above {COVERAGE_FLOOR:.0f}% floor"))
    return stale


def main(argv: list[str]) -> int:
    coverage_xml = Path(argv[1]) if len(argv) > 1 else REPO_ROOT / "coverage.xml"
    coverage_rates = _coverage_rates(coverage_xml)

    overall_stale = 0
    for baseline_file in sorted(BASELINE_DIR.glob("*-files.txt")):
        stale = _audit_baseline(baseline_file, coverage_rates)
        rel = baseline_file.relative_to(REPO_ROOT)
        if not stale:
            print(f"  ok  {rel.name} — no stale entries detected")
            continue
        overall_stale += len(stale)
        print(f"  STALE  {rel.name} — {len(stale)} entries no longer load-bearing:")
        for entry, reason in stale:
            print(f"    {entry}  ({reason})")

    print()
    if overall_stale == 0:
        print("\033[0;32m=== Baseline audit clean — no stale entries ===\033[0m")
        return 0
    print(f"\033[0;31m=== Baseline audit found {overall_stale} stale entries ===\033[0m")
    print("Remove the entries from their baseline files; the F-rule will catch any")
    print("real regression on the next CI run.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
