"""F21: Failure feedback in fitness-function check scripts must be agent-actionable.

Quality-harness failures only deliver value if the agent reading them
can take the next step *without re-reading the codebase*. The convention
we converged on with a sibling repo (see issue #258) is that every
emitted error string carries at least one of three lowercase action
markers — ``fix:``, ``next:``, ``run:``. Each marker means:

  - ``fix:`` — a sentence describing how to correct the violation.
  - ``next:`` — what to do after the fix (re-run a script, re-check, etc.).
  - ``run:`` — an exact command to copy-paste.

A check script that fails with a vague "AssertionError" or a remediation
that only describes the offence (but not the cure) is broken-by-design:
it wastes a loop while the agent re-derives an action from the
description. F21 detects that shape and fails it.

Detection (per scan target):

  * Python checks (``scripts/checks/check_*.py``):
      - Module-level string assignments named ``REMEDIATION``,
        ``REMEDIATION_TEXT``, ``MESSAGE``, etc. — the literal value
        must contain at least one marker.
      - Calls of the shape ``errors.append(<str>)`` /
        ``violations.append(<str>)`` / ``errors.extend([...])`` — every
        literal string argument must contain at least one marker.

  * Shell checks (``scripts/checks/check-*.sh``):
      - The shell variable ``REMEDIATION="..."`` (multi-line aware) and
        any here-doc / ``echo`` lines that look like failure output —
        the file body must contain at least one marker.

A file violates F21 if it has at least one remediation-shaped string
that lacks all three markers. Only ``check_*.py`` / ``check-*.sh`` /
``check_*.sh`` files are scanned — harness and tooling files
(``run-all.sh``, ``_lib.sh``, ``merge_coverage_xml.py``) are not
fitness checks and never match those globs.

There is no grandfathering: every offending check script fails the
gate until its remediation text is rewritten at source.

Dogfood: this file's own ``REMEDIATION`` MUST contain at least one of
``fix:``/``next:``/``run:``. If you sabotage it by removing them, F21
fires on itself.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import gate, repo_relative

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKS_DIR = REPO_ROOT / "scripts" / "checks"

# The three lowercase action markers a remediation/error string must
# carry at least one of (matched case-insensitively, so MarkdownBold
# variants still count — but the canonical form is lowercase).
ACTION_MARKERS: tuple[str, ...] = ("fix:", "next:", "run:")

# F21 extension (ADR-024 §"Agent UX / affordance principles"): every
# REMEDIATION string on a fitness-function check MUST also carry both
# of these substrings — Pass + Forbidden examples teach the agent the
# canonical shape of the cure AND the anti-pattern to avoid. F66 /
# F67 / F68 already follow this convention.
EXAMPLE_MARKERS: tuple[str, ...] = ("Pass example:", "Forbidden example:")

# Module-level string-constant names that, by convention, hold a
# failure-output / remediation message. Treated as remediation text and
# required to carry an action marker.
_REMEDIATION_CONSTANT_NAMES: frozenset[str] = frozenset(
    {
        "REMEDIATION",
        "REMEDIATION_TEXT",
        "MESSAGE",
        "ERROR_MESSAGE",
        "FAILURE_MESSAGE",
    }
)

# Local-variable names whose ``.append(...)`` / ``.extend(...)`` calls
# emit per-violation failure text. The literal-string contents of those
# calls must carry an action marker.
_ERROR_LIST_NAME_PATTERN = re.compile(r"^(errors?|violations?|failures?|problems?)$", re.IGNORECASE)

REMEDIATION = """F21: check-script failure output must be agent-actionable.

fix: rewrite the REMEDIATION constant (or appended error string) to
include at least one of the three lowercase markers — ``fix:``,
``next:``, or ``run:`` — so the agent reading the failure can take
the correction step without re-deriving it. The REMEDIATION constant
must ALSO include both ``Pass example:`` and ``Forbidden example:``
substrings (ADR-024 §"Agent UX / affordance principles" — Pass +
Forbidden teach the cure AND the anti-pattern in one place).

next: re-run ``python3 scripts/checks/check_actionable_feedback.py``
to confirm the gate goes green.

run: bash scripts/checks/run-all.sh

Pass example:

  REMEDIATION = '''Refactor to constructor-injected fakes to pass.

  fix: take the dependency as a kwarg of the unit under test and pass
  a Fake* from tests/fakes.py.
  next: re-run pytest tests/<dir>/ to confirm green.
  run: bash scripts/safe-commit.sh "test(<area>): inject fake instead of patch"

  Pass example: tests/contracts/test_source_connector_failure_modes.py
    def test_fetch_raises_propagates_to_dead_letter():
        source = FakeSourceConnector(fail_on_fetch={"item-001"}, ...)
        pipeline = build_connector_pipeline(...)
        pipeline.run_batch(source, FakeExtractor())
        assert db.execute("SELECT COUNT(*) FROM connector_deadletter").fetchone()[0] == 1

  Forbidden example: tests/contracts/test_some_thing.py
    @patch("kairix.core.connectors.pipeline.ConnectorPipeline._process_item")
    def test_pipeline_dead_letters(mock_process):
        # internal patching — F1 violation AND a shape-only proof.
        ...
  '''

Forbidden example:

  REMEDIATION = "This check failed. Some files violate the rule."
  # No fix:/next:/run: markers, no Pass / Forbidden example — the
  # agent reading this knows there is a problem but not what to do.

Why: a remediation that only describes the offence wastes one full
agent loop while the action is re-derived from the description. The
markers are a contract: when the gate fires, the cure is in the text."""


def _literal_text(node: ast.AST) -> str | None:
    """Return the literal string value of ``node`` if it's a constant
    or an f-string whose static parts can be flattened, else ``None``.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                # Treat the static surrounding text as the message; the
                # FormattedValue itself is a runtime hole, so we just
                # contribute an empty string for it.
                parts.append("")
        return "".join(parts)
    return None


def _has_action_marker(text: str) -> bool:
    """True iff ``text`` contains at least one of ``fix:``/``next:``/``run:``
    (case-insensitive)."""
    lowered = text.lower()
    return any(marker in lowered for marker in ACTION_MARKERS)


def _has_both_example_markers(text: str) -> bool:
    """True iff ``text`` contains BOTH ``Pass example:`` and ``Forbidden example:``.

    Case-insensitive match — the canonical form uses Title-cased
    Pass / Forbidden but a lowercase variant is acceptable. Both
    markers must appear so the remediation teaches the cure AND the
    anti-pattern (ADR-024 §"Agent UX / affordance principles").
    """
    lowered = text.lower()
    return all(marker.lower() in lowered for marker in EXAMPLE_MARKERS)


def _remediation_constants(tree: ast.AST) -> list[tuple[int, str]]:
    """Module-level ``NAME = "literal"`` assignments where NAME matches
    a known remediation-constant name. Returns (lineno, literal)."""
    found: list[tuple[int, str]] = []
    if not isinstance(tree, ast.Module):
        return found
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        text = _literal_text(node.value)
        if text is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in _REMEDIATION_CONSTANT_NAMES:
                found.append((getattr(node, "lineno", 0), text))
                break
    return found


def _appended_error_strings(tree: ast.AST) -> list[tuple[int, str]]:
    """Calls of the shape ``<errors>.append(<str-literal>)`` /
    ``<errors>.extend([<str-literal>, ...])`` where the receiver name
    matches an error-list pattern. Returns (lineno, literal)."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in {"append", "extend"}):
            continue
        receiver = func.value
        if not isinstance(receiver, ast.Name):
            continue
        if not _ERROR_LIST_NAME_PATTERN.match(receiver.id):
            continue
        for arg in node.args:
            text = _literal_text(arg)
            if text is not None:
                found.append((getattr(node, "lineno", 0), text))
                continue
            if isinstance(arg, (ast.List, ast.Tuple)):
                for item in arg.elts:
                    item_text = _literal_text(item)
                    if item_text is not None:
                        found.append((getattr(node, "lineno", 0), item_text))
    return found


def _python_file_violates(path: Path) -> bool:
    """True if any remediation/error string literal in this Python check
    file lacks the required markers.

    Two checks combine:

    1. Every emitted remediation/error string (constants + error-list
       appends) must carry at least one of the three lowercase action
       markers — ``fix:`` / ``next:`` / ``run:``.

    2. Every module-level REMEDIATION constant must ALSO carry both
       ``Pass example:`` AND ``Forbidden example:`` substrings — so
       the agent reading the failure learns both the canonical cure
       AND the anti-pattern to avoid (ADR-024 §"Agent UX / affordance
       principles"). Per-line appended error strings (which are
       per-violation context, not the canonical remediation template)
       are NOT required to carry the example markers.

    A file with NO detectable remediation text at all is treated as a
    violation — "silent" check scripts can't sneak in without an
    action-marker-carrying message.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    remediations = _remediation_constants(tree)
    appends = _appended_error_strings(tree)

    candidates = remediations + appends
    if not candidates:
        return True
    if any(not _has_action_marker(text) for _line, text in candidates):
        return True
    # Extension: module-level REMEDIATION constants must ship Pass +
    # Forbidden examples. The per-line appends aren't held to this
    # bar (they're contextual flavour, not the canonical template).
    return any(not _has_both_example_markers(text) for _line, text in remediations)


# Pattern that lifts plausible failure-output text out of a shell
# check script: REMEDIATION="..." assignments, echo lines, and here-doc
# bodies. We're deliberately coarse — we just want to know whether any
# substantive failure text in the file mentions a marker.
_SHELL_FAILURE_TEXT_PATTERN = re.compile(
    r"""(?xs)
    (?:                          # REMEDIATION="..." (single- or multi-line)
        \bREMEDIATION\s*=\s*"
        (?P<rem>[^"\\]*(?:\\.[^"\\]*)*)
        "
    )
    |
    (?:                          # echo "..." / printf "..." lines
        \b(?:echo|printf)\b[^\n]*
    )
    |
    (?:                          # here-doc bodies (best-effort)
        <<['"]?\w+['"]?\s*\n
        (?P<heredoc>[\s\S]*?)
        ^\w+\s*$
    )
    """,
    re.MULTILINE,
)


def _shell_file_violates(path: Path) -> bool:
    """True if the shell check script has remediation-shaped text that
    lacks every action marker. The detector is intentionally lenient:
    we only flag a file when it has a long-enough message but no marker
    anywhere in the file body. Short utility echoes ("=== running ===")
    don't qualify as remediation text.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False

    # Look for a REMEDIATION="..." block first — that's the canonical
    # remediation channel in our shell checks.
    rem_match = re.search(
        r'\bREMEDIATION\s*=\s*"(?P<body>[^"\\]*(?:\\.[^"\\]*)*)"',
        source,
        flags=re.DOTALL,
    )
    if rem_match is not None:
        return not _has_action_marker(rem_match.group("body"))

    # No REMEDIATION="..." present — fall back to scanning the whole
    # script for any agent-actionable line. If the file is purely a
    # delegation harness (e.g. ``exec python3 …``) with no failure text
    # of its own, treat that as a violation so that bare shell wrappers
    # can't bypass the rule.
    has_marker = _has_action_marker(source)
    if has_marker:
        return False
    # Tolerate files that delegate entirely to a python check — those
    # python files carry the marker. A delegating shell wrapper is
    # recognised by the presence of an explicit ``python3 .../check_*.py``
    # invocation.
    if re.search(r"python3\s+\S*check_\w+\.py", source):
        return False
    return True


def _collect_violations() -> set[Path]:
    """Walk every check_*.{py,sh} (and check-*.sh) under scripts/checks/
    and return repo-relative paths of files that violate F21.

    F21 deliberately scans itself: dogfood means the detector's own
    REMEDIATION must satisfy the rule. Sabotaging this file (removing
    the three markers from its REMEDIATION) must cause F21 to flag
    itself as a violation.
    """
    violations: set[Path] = set()

    for path in sorted(CHECKS_DIR.glob("check_*.py")):
        if _python_file_violates(path):
            violations.add(repo_relative(path))

    for pattern in ("check-*.sh", "check_*.sh"):
        for path in sorted(CHECKS_DIR.glob(pattern)):
            if _shell_file_violates(path):
                violations.add(repo_relative(path))

    return violations


def main() -> int:
    violations = _collect_violations()
    return gate("actionable-feedback", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
