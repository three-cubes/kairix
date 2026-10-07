"""F72: every named cross-layer integrity invariant has a matching test file.

Motivation (ADR-024 Bundle E)
-----------------------------
The existing test pyramid (Unit / Contract / Integration / BDD / E2E)
covers happy-path shape compliance and isolated unit logic. None of
those tiers systematically targets the failure class that produced the
SharePoint "5,200 bronze-but-not-content limbo" defect: state where
adjacent tables disagree about what's been processed. The integration
tests asserted "bronze got written" and "content got written"; nothing
asserted "bronze count == content distinct hashes + dead-letter
distinct items + in-flight". F72 makes that cross-layer integrity
contract structural.

Each invariant is its own file under ``tests/integrity_invariants/``
with two functions:

* ``test_invariant_holds_at_fixture_scale`` — N=10-100 rows, runs in
  CI Stage 3 (integration). Fast feedback loop on shape regressions.
* ``test_invariant_holds_at_soak_scale`` — N=10**4+ rows, carries
  ``@pytest.mark.soak`` so it runs only in the nightly soak workflow
  (excluded from per-commit CI). Catches scale-only regressions where
  the fixture-size variant is mute.

Both functions carry ``@pytest.mark.invariant`` (module-level
``pytestmark`` is acceptable). The soak variant additionally carries
``@pytest.mark.soak``.

The five seed invariants come from
``scripts/checks/_integrity_invariants_registry.py``:

  1. ``bronze_coverage_parity`` — bronze ↔ content ↔ dead-letter triple
  2. ``content_vectors_alignment`` — every vector traces to content
  3. ``staging_drain_progress`` — pushed_to_<sink>=0 stays at true scale
  4. ``documents_media_extractor_completeness`` — extracted → media row
  5. ``cc_pair_lifecycle_consistency`` — multi-tick state-machine integrity

Detection
---------
1. Read the registry from ``scripts/checks/_integrity_invariants_registry.py``.
2. For each invariant name, require:
   a. ``tests/integrity_invariants/test_<name>.py`` exists.
   b. The file defines a function ``test_invariant_holds_at_fixture_scale``.
   c. The file defines a function ``test_invariant_holds_at_soak_scale``.
   d. Both functions carry the ``invariant`` marker (directly via
      ``@pytest.mark.invariant`` or transitively via module-level
      ``pytestmark = pytest.mark.invariant``).
   e. The soak variant also carries the ``soak`` marker.

Violations are reported with a synthetic path key
``tests/integrity_invariants/test_<name>.py::<missing_requirement>`` so
the failure output lists "what's broken" in human-readable form.
There is no grandfathering: every violation fails the gate.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

INVARIANT_TESTS_ROOT = Path("tests") / "integrity_invariants"
REGISTRY_PATH = Path("scripts") / "checks" / "_integrity_invariants_registry.py"

# Required test function names per invariant — both must exist in the
# matching ``test_<name>.py`` module.
_FIXTURE_FN = "test_invariant_holds_at_fixture_scale"
_SOAK_FN = "test_invariant_holds_at_soak_scale"

# Required pytest markers. ``invariant`` belongs to both functions;
# ``soak`` belongs only to the soak-scale variant so the nightly soak
# workflow can include it via ``pytest -m soak`` while CI Stage 3
# excludes it via ``pytest -m "integration and not soak"``.
_MARKER_INVARIANT = "invariant"
_MARKER_SOAK = "soak"


REMEDIATION = """F72: integrity invariant '<name>' is missing its matching test
file at tests/integrity_invariants/test_<name>.py — the cross-layer
integrity contract from ADR-024 §F72 is not mechanically asserted.

This is the failure class that masked the "5,200 SharePoint items in
bronze-but-not-content limbo" defect. The integration tests proved
``bronze.write`` and ``content INSERT`` each ran; nothing proved their
counts agreed after a full batch. F72's per-invariant test files close
that gap.

fix: create tests/integrity_invariants/test_<name>.py with the
canonical shape:

    \"\"\"Invariant: <one-line operator-language description from
    scripts/checks/_integrity_invariants_registry.py>.

    Why: <link to the defect class that motivated this invariant —
    ADR-024 §"Defects that told us where the pyramid is wrong">.
    \"\"\"
    from __future__ import annotations

    from pathlib import Path

    import pytest

    pytestmark = pytest.mark.invariant


    def test_invariant_holds_at_fixture_scale(tmp_path: Path) -> None:
        # Seed cross-layer state at N=10-100 fixture rows.
        # Compose the production pipeline via kairix.core.factory.build_*
        # (F47-compliant). Run one or more ticks. Assert the invariant.
        ...


    @pytest.mark.soak
    def test_invariant_holds_at_soak_scale(tmp_path: Path) -> None:
        # Same shape, N>=10**4 rows. Carries @pytest.mark.soak so the
        # nightly soak workflow picks it up; CI Stage 3 skips it.
        ...

next: re-run python3 scripts/checks/check_f72_integrity_invariants.py
run: bash scripts/safe-commit.sh "test(integrity_invariants): add <name>"

Pass example: tests/integrity_invariants/test_bronze_coverage_parity.py

    pytestmark = pytest.mark.invariant

    def test_invariant_holds_at_fixture_scale(tmp_path):
        db = _open_db(tmp_path)
        pipeline = build_connector_pipeline(db=db, collection="x", ...)
        pipeline.run_batch(FakeSourceConnector(events=[...]), FakeExtractor())
        bronze_count = db.execute("SELECT COUNT(*) FROM bronze_records").fetchone()[0]
        content_distinct = db.execute(
            "SELECT COUNT(DISTINCT hash) FROM content").fetchone()[0]
        dead_letter = db.execute(
            "SELECT COUNT(DISTINCT item_id) FROM connector_deadletter").fetchone()[0]
        assert bronze_count == content_distinct + dead_letter, (
            f"bronze={bronze_count} != content_distinct={content_distinct} "
            f"+ dead_letter={dead_letter} — the limbo failure mode"
        )

Forbidden example: a test that only asserts ``bronze_count > 0``.
That's shape compliance — the limbo state passes (bronze has rows,
content has rows; nobody checked the rows agree).

Allowed exemption (rare): if an invariant is genuinely fixture-only
testable, add an ``# F72-soak-exempt: <rationale>`` comment immediately
above the missing soak variant — but expect pushback at review time.
The soak variant is what catches the scale-only regressions the
fixture variant can't see.
"""


def _load_registry(repo_root: Path) -> dict[str, str]:
    """Import the invariants registry module and return its INVARIANTS dict."""
    registry_file = repo_root / REGISTRY_PATH
    if not registry_file.exists():
        return {}
    spec = importlib.util.spec_from_file_location("_integrity_invariants_registry", registry_file)
    if spec is None or spec.loader is None:
        return {}
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return {}
    invariants = getattr(module, "INVARIANTS", None)
    if not isinstance(invariants, dict):
        return {}
    return dict(invariants)


def _harvest_module_pytestmark(tree: ast.Module) -> set[str]:
    """Return the set of pytest markers from any module-level ``pytestmark``.

    Handles three shapes:
      * ``pytestmark = pytest.mark.invariant``  → {"invariant"}
      * ``pytestmark = [pytest.mark.invariant, pytest.mark.slow]`` → {"invariant", "slow"}
      * ``pytestmark = pytest.mark.invariant``  → {"invariant"}

    Anything else returns the empty set.
    """
    markers: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name) and t.id == "pytestmark"]
        if not targets:
            continue
        value = node.value
        if isinstance(value, (ast.List, ast.Tuple)):
            for elt in value.elts:
                name = _extract_marker_name(elt)
                if name is not None:
                    markers.add(name)
        else:
            name = _extract_marker_name(value)
            if name is not None:
                markers.add(name)
    return markers


def _extract_marker_name(node: ast.expr) -> str | None:
    """Return the marker name from a ``pytest.mark.<name>`` attribute chain.

    Returns ``None`` for any other shape (a plain ``pytest.mark`` call
    with arguments, a bare ``Name``, etc.). ``invariant`` and ``soak``
    are simple attribute markers — no call form needed.
    """
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute):
        # pytest.mark.<name>
        if isinstance(node.value.value, ast.Name) and node.value.value.id == "pytest" and node.value.attr == "mark":
            return node.attr
    if isinstance(node, ast.Call):
        # pytest.mark.<name>(...)
        return _extract_marker_name(node.func)
    return None


def _function_markers(fn: ast.FunctionDef) -> set[str]:
    """Return the set of pytest markers carried by ``@pytest.mark.<name>`` decorators."""
    markers: set[str] = set()
    for deco in fn.decorator_list:
        name = _extract_marker_name(deco)
        if name is not None:
            markers.add(name)
    return markers


def _harvest_test_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Return ``{function_name: FunctionDef}`` for every top-level ``test_*``."""
    out: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            out[node.name] = node
    return out


def _check_one_invariant(
    name: str,
    repo_root: Path,
) -> list[str]:
    """Return the list of missing-requirement keys for one invariant.

    Each entry is a short descriptor like ``missing_file`` or
    ``missing_fixture_test`` so the violation surface tells the operator
    exactly what to add.
    """
    test_file = repo_root / INVARIANT_TESTS_ROOT / f"test_{name}.py"
    if not test_file.exists():
        return ["missing_file"]
    try:
        source = test_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ["unreadable_file"]
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ["unparseable_file"]
    module_markers = _harvest_module_pytestmark(tree)
    functions = _harvest_test_functions(tree)
    missing: list[str] = []
    if _FIXTURE_FN not in functions:
        missing.append("missing_fixture_test")
    else:
        fn_markers = _function_markers(functions[_FIXTURE_FN]) | module_markers
        if _MARKER_INVARIANT not in fn_markers:
            missing.append("missing_invariant_marker_on_fixture_test")
    if _SOAK_FN not in functions:
        missing.append("missing_soak_test")
    else:
        fn_markers = _function_markers(functions[_SOAK_FN]) | module_markers
        if _MARKER_INVARIANT not in fn_markers:
            missing.append("missing_invariant_marker_on_soak_test")
        if _MARKER_SOAK not in fn_markers:
            missing.append("missing_soak_marker_on_soak_test")
    return missing


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Walk every registered invariant; flag missing files / functions / markers.

    Returns repo-relative synthetic paths of the form
    ``tests/integrity_invariants/test_<name>.py::<missing>`` so each
    distinct missing requirement appears as its own violation.
    """
    invariants = _load_registry(repo_root)
    if not invariants:
        # No registry → no invariants registered → nothing to check.
        # This is the vacuous-green state before the registry file
        # lands; the F72 check stays inert in that window.
        return set()
    violations: set[Path] = set()
    for name in sorted(invariants):
        missing = _check_one_invariant(name, repo_root)
        for descriptor in missing:
            violations.add(Path(f"{INVARIANT_TESTS_ROOT}/test_{name}.py::{descriptor}"))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f72-integrity-invariants", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
