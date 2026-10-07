"""F86: DI-default execution floor — ``_default_*`` seams stay coverage-visible.

Motivation (EPIC #499 Phase 1; escape 4 — the terminal wizard SystemExit)
-------------------------------------------------------------------------
kairix wires production behaviour through ``_default_*`` DI-default
callables: the lazy-import delegation seams that ``SetupServiceDeps`` /
``EmbedDependencies`` / the extractor and probe factories bind unless a
test injects a fake. They ARE the production path — the function a real
operator runs when nothing overrides the seam.

When a ``_default_*`` seam carries ``# pragma: no cover`` it vanishes from
the per-file coverage floor (F7 / F9). A pragma'd seam can crash in
production with every suite green: escape 4 shipped exactly this way —
the terminal wizard's production-default embed callable was pragma'd, no
test ever executed it, and a ``SystemExit`` crash reached operators
behind a fully green gate. F86 makes the DI-default seam structurally
visible to the coverage floor (static half) AND structurally executed by
the suite (dynamic half).

The rule — two halves
---------------------
**STATIC half** (the per-commit gate; this script's default mode). Every
module-level or method ``_default_*`` function def in ``kairix/**`` may
NOT carry ``# pragma: no cover`` anywhere in its signature-or-body line
range. A pragma'd ``_default_*`` is invisible to F7/F9 — that invisibility
is the escape-4 mechanism. Self-contained AST harvest; no coverage report
needed; runs in Stage 0 via ``run-all.sh``.

**DYNAMIC half** (the union-coverage stage; ``--coverage-xml`` mode, the
F9 convention). Given a Cobertura ``coverage.xml`` (path from
``KAIRIX_COVERAGE_XML`` env, the ``--coverage-xml`` flag, or the
``coverage.xml`` fallback), every harvested ``_default_*`` function whose
body is PRESENT in the report must have ≥1 EXECUTED line. A seam that is
instrumented (its body lines appear in the report) but every body line
shows ``hits=0`` is never run by any test — the escape-4 shape one
ratchet removed from the static pragma (it has no pragma, but no test
exercises it either). When no coverage report is present the dynamic half
SKIPS clean — exactly as F7/F9's coverage checks do — so it never blocks
a per-commit run that has no coverage artifact.

The two halves are orthogonal by design. A ``# pragma: no cover`` seam is
EXCLUDED from the report (``[tool.coverage.report] exclude_lines`` lists
``pragma: no cover``), so its body has zero lines in the report; the
dynamic half skips it (no body lines to assert on) and the STATIC half
owns it. A non-pragma'd seam IS instrumented; the dynamic half owns
whether it actually ran. Neither half double-counts the other's
violations.

Scope (conservative — a detector agents distrust is worse than none)
--------------------------------------------------------------------
Only functions whose name starts with ``_default_`` (the established
DI-default seam convention), defined under ``kairix/**`` (production
source, never tests), at module level OR as a method. That is the whole
harvest set.

Intentionally NOT caught
------------------------
  * **Non-``_default_*`` production seams.** A DI default named by any
    other convention (``_make_embed``, a bare ``embed_backend`` factory)
    is invisible to this rule. ``_default_*`` is the contract; reviewers
    hold the line on the naming when a new seam is introduced.
  * **Module-level pragma blocks.** A file-level ``# pragma: no cover``
    (or a ``[tool.coverage] exclude_lines`` config) that hides a whole
    module is out of scope — F86 reads per-function pragmas only, not
    coverage configuration. (No such module-level seam exists in
    ``kairix/**`` today; F7/F9's per-file floor would already flag it.)
  * **Other suppressions.** ``# type: ignore`` / ``# noqa`` / ``# nosec``
    on a ``_default_*`` are F3's concern, not F86's — F86 is exclusively
    about coverage visibility.
  * **Dynamic-half partial coverage.** The dynamic half asserts ≥1
    executed line in the body, not full per-function coverage (F7/F9 own
    the percentage floor). One executed line proves the seam is reachable
    and run; it does not prove every branch is.
  * **Closures / nested ``_default_*`` defs** are harvested too (the AST
    walk descends), but a nested seam that the coverage report attributes
    to its enclosing function's lines is covered transitively — accepted.

There is no grandfathering: every pragma'd DI-default seam blocks at
pre-commit / safe-commit / CI Stage 0 until it earns an executing test.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

DEFAULT_PREFIX = "_default_"
PRAGMA_TAG = "pragma: no cover"

STATIC_GATE = "f86"
DYNAMIC_GATE = "f86-dynamic"

REMEDIATION = """F86: a DI-default ``_default_*`` seam is hidden from the coverage
floor — the escape-4 class where the terminal wizard's production-default
embed callable was pragma'd, no test ever ran it, and a SystemExit crash
shipped behind a fully green suite.

fix (static half — pragma'd seam): a ``_default_*`` function is the
PRODUCTION path (the lazy-import delegation a real operator runs when no
test injects a fake). Remove the ``# pragma: no cover`` and add a test
that EXECUTES the seam through the public surface — drive the deps default
binding, or call the public caller with deps=None so the production
default resolves. If the seam genuinely cannot be executed in-process
(it shells out to a system service, opens a real native handle), refactor
it to a thin adapter and push the testable logic behind a deps seam — do
not re-pragma it. There is no baseline to fall back on — every seam is
fixed at source.

fix (dynamic half — seam present but never executed): some test must run
at least one line of the seam's body. Add an outcome test that binds the
production default (deps=None / the default factory kwarg) and asserts on
what the seam DID. The F7/F9 percentage floor and this execution floor
are complementary: F7/F9 measure how much; F86 measures whether the
DI-default path runs at all.
next: re-run python3 scripts/checks/check_f86_di_default_execution_floor.py
(static), and for the dynamic half run a coverage pass and re-run with
--coverage-xml <coverage.xml>. See escape 4 (EPIC #499 Phase 1) for the
post-mortem this rule mechanises.
run: bash scripts/safe-commit.sh "test(<area>): execute the <name> DI-default seam (#499 escape-4 class)"

Pass example:
  # kairix/platform/setup/backends.py — the production embed seam, NO pragma
  def _default_embed_pipeline(**kwargs: Any) -> Any:
      from kairix.core.embed.use_cases import run_incremental_embed_pipeline
      return run_incremental_embed_pipeline(**kwargs)

  # tests/.../test_first_index.py — EXECUTES the seam's body:
  def test_first_index_runs_the_production_embed_default():
      # deps=None binds _default_embed_pipeline; the recorder proves it ran
      run_first_index(pipeline_fn=_recording_embed)   # default path covered

Forbidden example:
  # the escape-4 shape: a production seam hidden from the floor by the
  # coverage pragma (shown as <no-cover> here so this very docstring
  # doesn't trip F3 — the real offender writes the literal pragma comment).
  def _default_embed_pipeline(**kwargs):  # <no-cover pragma — hides the seam>
      from kairix.core.embed.use_cases import run_incremental_embed_pipeline
      return run_incremental_embed_pipeline(**kwargs)   # never run; ships a
                                                        # SystemExit crash
                                                        # behind a green suite."""


# ── harvest ────────────────────────────────────────────────────────────────


def _python_files(root: Path) -> list[Path]:
    """All ``.py`` files under ``root``, skipping ``__pycache__``."""
    if not root.exists():
        return []
    return [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]


def _default_funcs(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every ``_default_*`` function def in ``tree`` (module-level or
    method, sync or async; the AST walk descends into classes and nested
    scopes)."""
    out: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(DEFAULT_PREFIX):
            out.append(node)
    return out


def _func_line_range(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """``(def_line, end_line)`` — the signature-through-body span.

    Starts at the ``def`` line (decorators are excluded by design: a
    pragma on a decorator is not the seam-hiding shape F86 guards), ends
    at ``end_lineno`` so a pragma on a wrapped return-type continuation
    line (``) -> Path | None:  # pragma: no cover``) is seen.
    """
    start = node.lineno
    end = getattr(node, "end_lineno", None) or start
    return start, end


def _body_line_range(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int]:
    """``(first_body_line, end_line)`` — the executable-body span the
    dynamic half asks coverage about. The first statement's ``lineno``
    skips the signature so a covered-signature line (rare) is never
    mistaken for an executed body."""
    end = getattr(node, "end_lineno", None) or node.lineno
    first = node.body[0].lineno if node.body else node.lineno
    return first, end


# ── static half ─────────────────────────────────────────────────────────────


def _func_has_pragma(node: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]) -> bool:
    """True iff any line in the def's signature-or-body span carries
    ``# pragma: no cover``."""
    start, end = _func_line_range(node)
    for lineno in range(start, end + 1):
        if 0 < lineno <= len(source_lines) and PRAGMA_TAG in source_lines[lineno - 1]:
            return True
    return False


def collect_static_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Files with a pragma'd ``_default_*`` seam (the static half)."""
    violations: set[Path] = set()
    for path in _python_files(repo_root / "kairix"):
        text = path.read_text(encoding="utf-8")
        if DEFAULT_PREFIX not in text or PRAGMA_TAG not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        source_lines = text.splitlines()
        rel = path.relative_to(repo_root)
        for node in _default_funcs(tree):
            if _func_has_pragma(node, source_lines):
                start, _ = _func_line_range(node)
                violations.add(rel)
                print(f"  [f86] {rel}: line {start}: DI-default seam '{node.name}' carries '# {PRAGMA_TAG}'")
    return violations


# ── dynamic half ─────────────────────────────────────────────────────────────


def _parse_line_hits(coverage_xml: Path) -> dict[str, dict[int, int]]:
    """``{repo-relative-file: {line number: hit count}}`` from a Cobertura
    report — every instrumented line, hit or not.

    Mirrors ``check_per_file_coverage.parse_coverage``'s source-root
    handling so paths key as ``kairix/...`` regardless of whether the
    report stores filenames relative to the ``<source>`` root (the real
    ``source=["kairix"]`` config emits ``platform/setup/backends.py``) or
    already prefixed.

    The distinction between a line PRESENT with ``hits=0`` and a line
    ABSENT is load-bearing for the dynamic half: coverage instruments
    every executable line, so an un-run function's body lines are present
    with ``hits=0``, while a ``# pragma: no cover`` line is EXCLUDED from
    the report entirely (``[tool.coverage.report] exclude_lines`` lists
    ``pragma: no cover``). A pragma'd seam therefore has zero body lines
    in the report — that seam is the STATIC half's domain, not the
    dynamic half's.
    """
    tree = ET.parse(coverage_xml)
    root = tree.getroot()
    source_elements = [s.text for s in root.iter("source") if s.text]
    source_root = source_elements[0].strip("/") if source_elements else ""

    out: dict[str, dict[int, int]] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename") or ""
        if not filename:
            continue
        if source_root and not filename.startswith(source_root + "/"):
            full = f"{source_root}/{filename}"
        else:
            full = filename
        if not full.startswith("kairix/"):
            continue
        hits_by_line = out.setdefault(full, {})
        for line in cls.iter("line"):
            number = line.get("number")
            hits = line.get("hits", "0")
            if number is None:
                continue
            try:
                ln, h = int(number), int(hits)
            except ValueError:
                continue
            # Keep the max hit count if a line appears twice (merged reports).
            hits_by_line[ln] = max(hits_by_line.get(ln, 0), h)
    return out


def collect_dynamic_violations(coverage_xml: Path, repo_root: Path = REPO_ROOT) -> set[Path]:
    """Files with a ``_default_*`` seam whose body is PRESENT in the report
    but has 0 executed lines — the "instrumented but never run" escape.

    Restricted to files the report actually measured. A seam with NO body
    line in the report is skipped: that is either a module outside the
    report's ``<source>`` scope, or a ``# pragma: no cover`` seam whose
    lines coverage excluded — the latter is the static half's domain
    (this rule deliberately does not double-count it here)."""
    hits_by_file = _parse_line_hits(coverage_xml)
    violations: set[Path] = set()
    for path in _python_files(repo_root / "kairix"):
        text = path.read_text(encoding="utf-8")
        if DEFAULT_PREFIX not in text:
            continue
        rel = path.relative_to(repo_root)
        rel_key = str(rel)
        if rel_key not in hits_by_file:
            continue  # module not in this report's measurement scope
        line_hits = hits_by_file[rel_key]
        try:
            tree = ast.parse(text, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in _default_funcs(tree):
            first, end = _body_line_range(node)
            body_lines = [ln for ln in range(first, end + 1) if ln in line_hits]
            if not body_lines:
                continue  # pragma-excluded or unmeasured — static half's domain
            if all(line_hits[ln] == 0 for ln in body_lines):
                violations.add(rel)
                print(f"  [f86-dynamic] {rel}: line {node.lineno}: DI-default seam '{node.name}' body never executed")
    return violations


def _coverage_xml_path(explicit: str | None) -> Path | None:
    """Resolve the coverage report: ``--coverage-xml`` wins, then
    ``KAIRIX_COVERAGE_XML``, then a repo-root ``coverage.xml``. Returns
    ``None`` (→ dynamic half skips clean) when none exists — the F7/F9
    coverage-absent convention."""
    candidate_str = explicit or os.environ.get("KAIRIX_COVERAGE_XML")
    candidate = Path(candidate_str) if candidate_str else (REPO_ROOT / "coverage.xml")
    return candidate if candidate.exists() else None


# ── entry ────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_f86_di_default_execution_floor.py",
        description="F86 DI-default execution floor (#499 Phase 1) — static + dynamic halves.",
    )
    parser.add_argument(
        "--coverage-xml",
        nargs="?",
        const="",
        default=None,
        metavar="PATH",
        help=(
            "run the DYNAMIC half against this Cobertura report (or "
            "KAIRIX_COVERAGE_XML / coverage.xml). Omit the flag entirely to "
            "run the STATIC pragma half only."
        ),
    )
    args = parser.parse_args(argv)

    if args.coverage_xml is None:
        # Static half — the per-commit gate.
        return gate(STATIC_GATE, collect_static_violations(), REMEDIATION)

    # Dynamic half — the union-coverage stage.
    explicit = args.coverage_xml or None
    coverage_xml = _coverage_xml_path(explicit)
    if coverage_xml is None:
        print("notice [arch:f86-dynamic]: no coverage report found — dynamic half skipped (CI enforces it).")
        return 0
    print(f"[f86-dynamic] reading executed lines from {coverage_xml}")
    return gate(DYNAMIC_GATE, collect_dynamic_violations(coverage_xml), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
