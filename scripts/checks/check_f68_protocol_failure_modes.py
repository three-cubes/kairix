"""F68: every public Protocol method has a failure-injection contract test.

Motivation (ADR-024 Bundle A)
-----------------------------
Eight production-impact defects identified in 2026-05 all passed an
8000+ test suite. The common pattern: tests proved *shape compliance*
("the call returns the right type") but not *failure behaviour* ("when
this call raises / times out / returns empty, what does the caller
observe?"). Bug 2 — SharePoint 429 dead-lettered every item on a
throttled drive because no contract test exercised the rate-limit
path — is the canonical example.

F68 makes the failure-behaviour contract structural. For every public
method on every Protocol declared anywhere under ``kairix/**/*.py``,
the file ``tests/contracts/test_<protocol_snake>_failure_modes.py``
MUST exist AND contain at least one test function whose name matches::

    test_<method>_<failure_class>_<observable_outcome>

where ``<failure_class>`` is one of:

  * ``raises`` — the call raises an exception
  * ``times_out`` — the call exceeds a deadline
  * ``returns_partial`` — the call returns a truncated / incomplete result
  * ``returns_empty`` — the call returns an empty collection
  * ``unauthorized`` — the call fails authorisation (HTTP 401 / 403 shape)
  * ``unavailable`` — the call's dependency is unreachable (HTTP 503 shape)

Detection
---------
1. Walk every ``*.py`` under ``kairix/``. AST-scan for class
   definitions whose direct bases include ``Protocol`` — bare
   ``Protocol``, ``Protocol[...]``, or attribute form
   ``typing.Protocol`` / ``typing_extensions.Protocol`` (with or
   without ``@runtime_checkable`` decoration).

   Scope widening (#499 Phase 0, EPIC escape 2): the original detector
   walked only ``kairix/**/protocols.py``, so any Protocol declared in
   a regular module (``SetupService`` in
   ``kairix/platform/setup/service.py`` being the canonical escape —
   21 methods, zero failure-injection coverage, ``set_secret``'s
   multi-line rejection shipped unseen) was invisible to F68. The
   detector now discovers Protocols repo-wide; the Protocols it newly
   surfaced fail the gate like any other (there is no grandfathering).
   Method-level exclusions are unchanged: only
   public (non-underscore-prefixed) methods are governed.
2. For each Protocol class, enumerate every public method (no
   underscore prefix; ``FunctionDef`` or ``AsyncFunctionDef`` in the
   class body).
3. For each ``<ProtocolName>.<method>`` combination, compute the
   expected test-file path
   ``tests/contracts/test_<protocol_snake>_failure_modes.py`` and
   require it to contain at least one ``def test_<method>_(raises|...)_<rest>``
   declaration.
4. Combinations that fail the requirement are reported as
   ``<ProtocolName>.<method>``. Every Protocol method (existing or
   net-new) requires a matching failure-mode test; there is no
   grandfathering.

The detection deliberately enforces the function-name pattern (not just
"some test exists") so the test author has to name the failure class
they covered — which forces them to think about which class the
Protocol's surface actually exposes.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

KAIRIX_ROOT = Path("kairix")
TESTS_CONTRACTS = Path("tests") / "contracts"

# The fixed enum of failure classes a test function name may declare.
# Extending this set is a deliberate follow-up: new classes get added
# here only when a defect post-mortem surfaces a class the existing six
# don't cover.
FAILURE_CLASSES: tuple[str, ...] = (
    "raises",
    "times_out",
    "returns_partial",
    "returns_empty",
    "unauthorized",
    "unavailable",
)

REMEDIATION = """F68: Protocol <Name>.<method> has no failure-injection contract test.

This is the ADR-024 class — "shape compliance only, no failure-mode
coverage". Bug 2 (2026-05 SharePoint 429 dead-lettering every item)
shipped because no contract test exercised the rate-limit path; the
Protocol's ``fetch`` method had shape proofs but no behaviour proof
under throttle. F68 mechanically prevents the same class of regression
for every Protocol method on every Protocol surface.

fix: add a test to tests/contracts/test_<protocol_snake>_failure_modes.py
with a function name matching the F68 regex::

    test_<method>_(raises|times_out|returns_partial|returns_empty|unauthorized|unavailable)_<rest>

The test injects the failure through a canonical fake from tests/fakes.py
and asserts on a concrete observable outcome (a row count, a result
field, an exception type with message) — NOT a mock assertion or
``assert raised is True``.

Canonical test shape::

    @pytest.mark.contract
    def test_<method>_raises_propagates_to_dead_letter(tmp_path):
        db = sqlite3.connect(":memory:")
        create_schema(db)
        source = FakeSourceConnector(
            events=[ChangeEvent(...)],
            fail_on_<method>={"item-001"},  # or raise_on_<method>=RuntimeError(...)
        )
        pipeline = build_connector_pipeline(db=db, collection="default", ...)
        pipeline.run_batch(source, FakeExtractor())
        # CONCRETE assertion — sabotage-provable:
        rows = db.execute(
            "SELECT item_id FROM connector_deadletter WHERE source_name = ?",
            (source.name,),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "item-001"

next: re-run python3 scripts/checks/check_f68_protocol_failure_modes.py
run: bash scripts/safe-commit.sh "test(contracts): add <method> failure-mode contract for <Protocol>"

Pass example: tests/contracts/test_source_connector_failure_modes.py
  def test_fetch_raises_propagates_to_dead_letter(tmp_path):
      source = FakeSourceConnector(fail_on_fetch={"item-001"}, ...)
      pipeline = build_connector_pipeline(db=db, collection="default", ...)
      pipeline.run_batch(source, FakeExtractor())
      assert db.execute(
          "SELECT COUNT(*) FROM connector_deadletter"
      ).fetchone()[0] == 1

Forbidden example: tests/contracts/test_source_connector_protocol.py
  def test_fetch_returns_raw_artefact():
      # SHAPE-only proof: confirms the return type but proves nothing
      # about failure behaviour. F68 requires BEHAVIOUR under failure
      # too.
      raw = source.fetch("item-001")
      assert isinstance(raw, RawArtefact)

When the Protocol method's failure surface is genuinely empty (rare —
a pure-functional method with no I/O):

  def test_<method>_returns_empty_when_no_input_provided():
      ...

See docs/architecture/ADR-024-test-pyramid-redesign.md §F68 for the
full spec.
"""


def _to_snake_case(name: str) -> str:
    """Convert ``CamelCase`` Protocol name to ``snake_case`` test-file suffix.

    Mirrors PEP 8 module-name convention; runs of capitals collapse
    sensibly (``LLMJudge`` -> ``llm_judge``, ``OAuthConnector`` ->
    ``o_auth_connector``). The exact mapping matters only because the
    F68 test file location is mechanical — author and detector must
    agree.
    """
    out = re.sub(r"(?<!^)(?=[A-Z][a-z])", "_", name)
    out = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", out)
    return out.lower()


def _base_names_protocol(base: ast.expr) -> bool:
    """True iff a single base expression denotes ``Protocol``.

    Accepted shapes: ``Protocol`` (Name), ``typing.Protocol`` /
    ``typing_extensions.Protocol`` / any ``<mod>.Protocol`` attribute,
    and the subscripted generic form of each (``Protocol[T]``,
    ``typing.Protocol[T]``).
    """
    if isinstance(base, ast.Name):
        return base.id == "Protocol"
    if isinstance(base, ast.Attribute):
        return base.attr == "Protocol"
    if isinstance(base, ast.Subscript):
        return _base_names_protocol(base.value)
    return False


def _is_protocol_class(node: ast.ClassDef) -> bool:
    """Return True iff ``node`` declares ``Protocol`` (or ``Protocol[...]``) as a base.

    Mirrors typing.Protocol detection without importing the source — the
    AST tells us whether ``Protocol`` appears in the bases tuple
    (bare name, ``typing.Protocol`` / ``typing_extensions.Protocol``
    attribute form, or either subscripted). We deliberately don't
    require ``@runtime_checkable`` since many domain-internal
    Protocols skip it — decorated-or-not, the bases decide.
    """
    return any(_base_names_protocol(base) for base in node.bases)


def _public_methods(node: ast.ClassDef) -> list[str]:
    """Return the names of every public method on ``node``.

    Public = not underscore-prefixed AND a ``FunctionDef`` /
    ``AsyncFunctionDef``. Property descriptors (``@property``) count —
    they're still part of the Protocol's behavioural surface and can
    raise / return-empty just like normal methods.
    """
    out: list[str] = []
    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if item.name.startswith("_"):
            continue
        out.append(item.name)
    return out


def _harvest_protocols(repo_root: Path) -> dict[str, list[str]]:
    """Walk every ``*.py`` under ``kairix/`` and return
    ``{ProtocolName: [method, ...]}`` for each Protocol-derived class.

    Widened from ``*/protocols.py`` to the full tree (#499 Phase 0) —
    see the module docstring for the SetupService escape that motivated
    it. Duplicate Protocol names across files collapse to one entry
    (last write wins) — in practice kairix has no name collisions.
    """
    out: dict[str, list[str]] = {}
    kairix_dir = repo_root / KAIRIX_ROOT
    if not kairix_dir.exists():
        return out
    for path in kairix_dir.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not _is_protocol_class(node):
                continue
            methods = _public_methods(node)
            if methods:
                out[node.name] = methods
    return out


def _build_test_function_pattern(method: str) -> re.Pattern[str]:
    """Compile the regex that matches a function name covering ``method``.

    The pattern is anchored: ``^test_<method>_(raises|...)_.*$``. The
    trailing ``.*`` requires the author to name an observable outcome
    suffix (e.g. ``..._propagates_to_dead_letter``) so the test name is
    informative, not just ``test_fetch_raises``.
    """
    classes = "|".join(FAILURE_CLASSES)
    return re.compile(rf"^test_{re.escape(method)}_({classes})_.+$")


def _harvest_test_functions(repo_root: Path, protocol_snake: str) -> list[str]:
    """Return every ``def test_*`` name in the protocol's failure-mode test file.

    Returns ``[]`` when the file does not exist OR cannot be parsed —
    both shapes are treated as "no coverage", which is the F68 violation.
    """
    test_file = repo_root / TESTS_CONTRACTS / f"test_{protocol_snake}_failure_modes.py"
    if not test_file.is_file():
        return []
    try:
        tree = ast.parse(test_file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            names.append(node.name)
    return names


def _method_is_covered(method: str, test_function_names: list[str]) -> bool:
    """True iff at least one test name matches the F68 regex for ``method``."""
    pattern = _build_test_function_pattern(method)
    return any(pattern.match(name) for name in test_function_names)


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Return repo-relative ``Path`` entries for every uncovered combination.

    The synthetic path encodes ``<ProtocolName>.<method>`` so the failure
    output is human-readable. Operators remediate by adding the
    failure-mode test.
    """
    protocols = _harvest_protocols(repo_root)
    violations: set[Path] = set()
    # Cache per-protocol test-function listing so we read each file once.
    snake_cache: dict[str, list[str]] = {}
    for proto_name, methods in protocols.items():
        snake = _to_snake_case(proto_name)
        if snake not in snake_cache:
            snake_cache[snake] = _harvest_test_functions(repo_root, snake)
        test_function_names = snake_cache[snake]
        for method in methods:
            if not _method_is_covered(method, test_function_names):
                violations.add(Path(f"{proto_name}.{method}"))
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("f68-protocol-failure-modes", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
