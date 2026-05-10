"""F7 / F9: Per-file coverage floor at 85%.

Repository-wide coverage averages can hide files with 0% coverage.
This check enforces a per-file floor: every kairix/* source file in
the ``coverage.xml`` report must be ≥85% covered.

Modes:

  - **F7 (unit only)**: invoked with one argument or no argument —
    defaults gate name to ``per-file-coverage-floor`` and reads
    baseline from ``per-file-coverage-floor-files.txt``. Reflects
    unit + bdd + contract coverage from Stage 2.

  - **F9 (union)**: invoked with a second positional argument naming
    a different gate (e.g. ``per-file-coverage-floor-union``). Used
    against a coverage XML produced by ``coverage combine`` over
    unit + integration ``.coverage`` files. Reflects the union of
    all test scopes; production-wiring files exercised only at
    integration scope no longer measure as uncovered.

Files currently below the floor for a given gate are listed in
``.architecture/baseline/<gate-name>-files.txt``. The check fails if a
file NOT in that baseline is below the floor.

Existing baseline files are grandfathered. The expectation is the
baseline shrinks over time as testing improves.

Usage:
    python3 scripts/checks/check_per_file_coverage.py [coverage.xml] [gate-name]

If no path is given, defaults to ``coverage.xml`` in the CWD (the file
emitted by pytest --cov-report=xml). If no gate name is given, defaults
to ``per-file-coverage-floor`` (F7).
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

FLOOR = 85.0  # per-file coverage percentage threshold


REMEDIATION = f"""A file dropped below {FLOOR:.0f}% coverage. Add tests that
drive the public surface; do not add `# pragma: no cover` to silence the
gate. If the file genuinely cannot be covered (e.g. it is exclusively
production-infrastructure code that requires a live external service),
the right action is to extract the testable logic into a use-case class
and reduce the production-only file to a thin Adapter — not to suppress.

Per-file coverage is the right unit of measurement: a 91% repo average
can hide a file at 0%."""


def parse_coverage(coverage_xml: Path) -> dict[Path, float]:
    """Extract per-file line-rate (0..1) from a coverage.xml report.

    Cobertura XML uses ``<source>`` to declare the source root and then
    emits ``<class filename="...">`` paths *relative to that root*. We
    prepend the source root so paths are repo-relative (``kairix/foo.py``)
    matching how the baseline file stores them.
    """
    if not coverage_xml.exists():
        print(f"ERROR: coverage report not found at {coverage_xml}", file=sys.stderr)
        sys.exit(2)

    tree = ET.parse(coverage_xml)
    root = tree.getroot()

    # Read the source root (e.g. "kairix") so we can build repo-relative paths.
    source_elements = [s.text for s in root.iter("source") if s.text]
    if source_elements:
        # Coverage emits the source dir relative to repo root in the typical
        # `--cov=kairix` case. Use the first one.
        source_root = source_elements[0].strip("/")
    else:
        source_root = ""

    out: dict[Path, float] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename") or ""
        if not filename:
            continue
        # Build a repo-relative path. If filename is already prefixed with the
        # source dir (some coverage versions emit absolute-style paths) keep
        # it; otherwise prepend the source root.
        if source_root and not filename.startswith(source_root + "/"):
            full = f"{source_root}/{filename}"
        else:
            full = filename
        # Restrict to kairix/* — skip test files, scripts, etc.
        if not full.startswith("kairix/"):
            continue
        line_rate_str = cls.get("line-rate", "1.0")
        try:
            line_rate = float(line_rate_str)
        except ValueError:
            continue
        path = Path(full)
        prev = out.get(path)
        if prev is None or line_rate < prev:
            out[path] = line_rate
    return out


def main(argv: list[str]) -> int:
    coverage_xml = Path(argv[1]) if len(argv) > 1 else Path("coverage.xml")
    gate_name = argv[2] if len(argv) > 2 else "per-file-coverage-floor"
    coverage = parse_coverage(coverage_xml)

    if not coverage:
        print(f"ERROR: no kairix/* files found in {coverage_xml}", file=sys.stderr)
        return 2

    below_floor = {
        path  # already a relative Path because coverage.xml uses relative_files=true
        for path, rate in coverage.items()
        if rate * 100 < FLOOR
    }

    # Pretty-print percentages in the failure message
    if below_floor:
        # When run alone (not via run_all), print all percentages so the
        # operator can see how far each file is from the floor.
        lines: list[str] = []
        for path in sorted(below_floor):
            pct = coverage[path] * 100
            lines.append(f"  {path}  {pct:5.1f}%  (floor: {FLOOR:.0f}%)")
        formatted = "\n".join(lines)
        result = gate(gate_name, below_floor, REMEDIATION + "\n\nMeasured:\n" + formatted)
        return result

    return gate(gate_name, set(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
