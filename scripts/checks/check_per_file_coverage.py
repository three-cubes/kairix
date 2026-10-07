"""F7 / F9: Per-file coverage floor at 90%.

Repository-wide coverage averages can hide files with 0% coverage.
This check enforces a per-file floor: every kairix/* source file in
the ``coverage.xml`` report must be ≥90% covered.

Modes:

  - **F7 (unit only)**: invoked with one argument or no argument —
    defaults gate name to ``per-file-coverage-floor``. Reflects
    unit + bdd + contract coverage from Stage 2.

  - **F9 (union)**: invoked with a second positional argument naming
    a different gate (e.g. ``per-file-coverage-floor-union``). Used
    against a coverage XML produced by ``coverage combine`` over
    unit + integration ``.coverage`` files. Reflects the union of
    all test scopes; production-wiring files exercised only at
    integration scope no longer measure as uncovered.

The check fails if ANY kairix/* file in the report is below the floor.
There is no grandfathering (PLA-472 retired the per-file baselines): a
file below the floor is fixed at source.

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
from tc_fitness import gate

FLOOR = 90.0  # per-file coverage percentage threshold


REMEDIATION = f"""Per-file coverage below {FLOOR:.0f}% — but coverage % is a
SMELL, not the gate. Before padding tests to chase the floor, ask the
ADR-024 question first: what DEFECT CLASS is this file's coverage
gap actually a proxy for?

Per ADR-024 §"Repositioning F7", the right response depends on what
the gap signals:

  * Missing failure-mode coverage at a Protocol boundary
    → see F68 (Protocol failure-injection contract). Add a test in
      tests/contracts/test_<protocol_snake>_failure_modes.py per the
      F68 affordance template — that's the right shape, not a unit
      test that calls the function once to push the % up.

  * Missing scale-bound coverage at an iteration / persistence boundary
    → see F69 (scale-bound test). Add a 10**4-row variant to the
      relevant integration test per the F69 affordance template.

  * Genuinely production-only Adapter (dispatch + dep wiring; substantive
    logic delegated to tested modules)
    → put the wiring behind a Deps DI seam (see kairix/worker.py::WorkerDeps)
      so tests drive the dispatch branches with fakes, and cover the thin
      remaining glue with an integration test (it counts toward the F9
      union floor). There is no baseline to grandfather the file into.

fix: pick the right path above for THIS file's gap. Do NOT add a unit
test that calls the function once just to push coverage above {FLOOR:.0f}%
— that's the "gaming sonarcloud" anti-pattern ADR-024 names and
rejects. If a file is genuinely production-only infrastructure,
extract its testable logic into a use-case class per the kairix Deps
pattern and reduce the file to a thin Adapter; never use
``# pragma: no cover`` to silence the gate.

next: re-run ``pytest --cov=kairix --cov-report=xml`` then
``python3 scripts/checks/check_per_file_coverage.py`` to confirm the
gate goes green AFTER making the structural change (not coverage-padding).

run: bash scripts/safe-commit.sh "<verb>(<area>): <what you actually changed>"

Pass example:
  # The Neo4jDrainTickDeps DI pattern from kairix/core/curator/drain.py
  # (commit 651942cb). Production-default factories live on a frozen
  # dataclass; the orchestration function accepts deps=None and binds
  # the production defaults; tests pass deps=Neo4jDrainTickDeps(
  # client_factory=..., db_factory=..., repo_factory=...) to exercise
  # the orchestration branches without real Neo4j or live SQLite.
  # Reduces the orchestration to ~10 lines of branching that's fully
  # unit-test-reachable.

Forbidden example:
  # in kairix/core/search/pipeline.py — silencing the coverage gate
  def run(self, query: str) -> list[Hit]:
      ...  # <pragma: no cover>   # (with no rationale)

  # OR: gaming the floor by adding a test that calls the function
  # once just to push the percentage up without proving any
  # behaviour — see ADR-024 §"Repositioning F7" for why this is the
  # wrong fix.

Per-file coverage is the right unit of measurement: a 91% repo average
can hide a file at 0%. But coverage % is one signal; F68/F69 are the
right gates for the failure classes coverage gaps usually proxy."""


def parse_coverage(coverage_xml: Path) -> dict[Path, float]:
    """Extract per-file line-rate (0..1) from a coverage.xml report.

    Cobertura XML uses ``<source>`` to declare the source root and then
    emits ``<class filename="...">`` paths *relative to that root*. We
    prepend the source root so paths are repo-relative (``kairix/foo.py``)
    matching how the gate reports them.
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
