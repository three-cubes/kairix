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
that lacks all three markers AND the file is not in the allow-list:

  * ``scripts/checks/_arch_lib.py`` — shared helpers; the remediation
    text it prints is supplied by callers, not owned here.
  * ``scripts/checks/run-all.sh`` — orchestrator/harness; emits
    aggregate pass/fail messages, not per-rule remediation.
  * ``scripts/checks/_lib.sh`` — shared helpers (same rationale as
    ``_arch_lib.py``).
  * ``scripts/checks/audit_baselines.py`` — auditor over the baselines;
    not a fitness check itself.
  * ``scripts/checks/merge_coverage_xml.py`` — tool, not a check.

Baseline at ``.architecture/baseline/actionable-feedback-files.txt``
grandfathers existing offenders so the rule lands green; the baseline
is expected to shrink as remediation strings get rewritten to satisfy
the convention.

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
from _arch_lib import gate, repo_relative

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CHECKS_DIR = REPO_ROOT / "scripts" / "checks"

# The three lowercase action markers a remediation/error string must
# carry at least one of (matched case-insensitively, so MarkdownBold
# variants still count — but the canonical form is lowercase).
ACTION_MARKERS: tuple[str, ...] = ("fix:", "next:", "run:")

# Files inside scripts/checks/ that are NOT individual fitness checks
# and so are exempt from F21 — they don't own per-rule remediation.
_ALLOW_FILES: frozenset[str] = frozenset(
    {
        "_arch_lib.py",
        "_lib.sh",
        "run-all.sh",
        "audit_baselines.py",
        "merge_coverage_xml.py",
    }
)

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
``next:``, or ``run:`` — so the agent reading the failure can take the
correction step without re-deriving it.

next: re-run ``python3 scripts/checks/check_actionable_feedback.py``
to confirm the gate goes green.

run: bash scripts/checks/run-all.sh

Pass example (single marker satisfies the rule, but more context is
better — F15/F16/F20 use a richer "Pass example / Forbidden example"
extension):

  REMEDIATION = '''Refactor to constructor-injected fakes to pass.

  fix: take the dependency as a kwarg of the unit under test and pass
  a Fake* from tests/fakes.py.
  next: re-run pytest tests/<dir>/ to confirm green.
  run: bash scripts/safe-commit.sh "test(<area>): inject fake instead of patch"
  '''

Forbidden example (no marker — the agent reading this knows there is
*a* problem but not what to *do* about it):

  REMEDIATION = "This check failed. Some files violate the rule."

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
    file lacks every action marker. Constants and error-list appends are
    both inspected; a file with NO remediation strings at all is treated
    as a violation because every fitness check is expected to emit
    actionable failure text.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False

    remediations = _remediation_constants(tree)
    appends = _appended_error_strings(tree)

    # Every individual emitted string must carry at least one marker.
    candidates = remediations + appends
    if not candidates:
        # No detectable remediation text at all — treat as a violation
        # so that "silent" check scripts can't sneak in without an
        # action-marker-carrying message.
        return True
    return any(not _has_action_marker(text) for _line, text in candidates)


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
    itself as a net-new violation.
    """
    violations: set[Path] = set()

    for path in sorted(CHECKS_DIR.glob("check_*.py")):
        if path.name in _ALLOW_FILES:
            continue
        if _python_file_violates(path):
            violations.add(repo_relative(path))

    for pattern in ("check-*.sh", "check_*.sh"):
        for path in sorted(CHECKS_DIR.glob(pattern)):
            if path.name in _ALLOW_FILES:
                continue
            if _shell_file_violates(path):
                violations.add(repo_relative(path))

    return violations


def main() -> int:
    violations = _collect_violations()
    return gate("actionable-feedback", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
