"""F83: gate-runner contract for shell gate scripts.

Motivation (EPIC #499 Phase 0; the #483 silent-death class)
-----------------------------------------------------------
``safe-commit.sh``'s test stage died silently for months: under
``set -e``, a ``VAR=$(cmd)`` capture kills the whole script AT THE
ASSIGNMENT when ``cmd`` exits non-zero — no FAIL line, no captured
output; the log just ends at the stage's ``...`` prefix. F83 makes the
post-#483 hardening structural for every shell script under
``scripts/`` and ``scripts/checks/``.

Sub-rules
---------
(a) **No unguarded capture under ``set -e``.** In any file that enables
    errexit (``set -e`` / ``set -eu`` / ``set -euo pipefail`` /
    ``set -o errexit``), a statement-level ``VAR=$(...)`` assignment
    must carry an ``||`` guard on the same logical line (backslash
    continuations and multi-line ``$(...)`` are joined first), or a
    ``# F83-allowed: <why>`` rationale.
(b) **``|| true`` requires a trailing rationale comment** on the same
    line — a silent ignore without a stated reason is how expected
    failures and real failures become indistinguishable.
(c) **shellcheck clean at error severity** for every in-scope file
    (skipped with a notice when shellcheck is not installed — CI has it).
(d) **Stage-verdict ledger** — targeted at ``scripts/safe-commit.sh``
    and ``scripts/checks/run-all.sh`` only:

    * ``safe-commit.sh`` convention (pinned from the post-#483 file):
      every gate stage announces itself with ``echo -n "  <stage>... "``
      and the stage block (up to the next announcement) must emit BOTH
      a success verdict containing ``OK${NC}`` and a failure verdict
      containing ``FAIL${NC}`` (a ``gate_died`` call counts — it prints
      the stage-named FAIL line).
    * ``run-all.sh`` convention: every check invocation
      (``python3 "${SCRIPT_DIR}/check_*.py"`` / ``bash
      "${SCRIPT_DIR}/check-*.sh"``) carries ``|| overall=1`` so one
      failing check never aborts the ledger, and the aggregate
      pass/FAIL verdict lines exist.
(e) **No quiet-grep output probes in pipelines under ``pipefail``.**
    ``producer | grep -q`` can report failure after a successful match:
    ``grep -q`` exits early, the producer receives SIGPIPE, and
    ``pipefail`` returns the producer's non-zero status. Probe captured
    output with a here-string instead: ``grep -q PATTERN <<< "$OUTPUT"``.

Intentionally NOT caught (precision over recall):

  * ``local``/``export``/``declare`` capture (return-code swallowing,
    SC2155) — a different class; shellcheck warns on it.
  * Prologue idioms ``VAR="$(cd ... && pwd)"`` / ``$(dirname ...)`` /
    ``$(basename ...)`` / ``$(pwd)`` — they fail loudly on their own
    stderr and appear in every script's header.
  * ``set +e`` re-enable windows — a file is treated as errexit-governed
    if ANY ``set -e`` appears.
  * ``$(...)`` inside ``if``/``while`` conditions — errexit does not
    apply there.
  * ``$((...))`` arithmetic expansion — cannot trip errexit at an
    assignment.
  * Any ``||`` on the logical line counts as a guard, including ``||``
    that only appears inside the substitution (``VAR=$(cmd || true)``
    is genuinely safe) and ``||`` embedded in program text (a sed
    script like ``s|x$||`` slips through) — accepted under-catch.

The per-file baseline ``.architecture/baseline/f83-files.txt``
grandfathers pre-existing offenders. NOTE: the baseline masks ALL F83
sub-rules for a listed file — paying a file down means satisfying every
sub-rule (refinement to per-finding granularity is #499 Phase 2).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from tc_fitness import REPO_ROOT, gate

RATIONALE_TAG = "# F83-allowed:"

SET_E_RE = re.compile(r"^\s*set\s+(?:-[a-zA-Z]*e[a-zA-Z]*\b|-o\s+errexit\b)", re.MULTILINE)
# `$((...))` is arithmetic expansion, not command substitution — it
# cannot trip errexit at an assignment, so the lookahead excludes it.
ASSIGN_CAPTURE_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\+?=[\"']?\$\((?!\()")
EXEMPT_CAPTURE_RE = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*\+?=[\"']?\$\(\s*(?:cd\s|dirname\s|basename\s|pwd\b)")
OR_TRUE_RE = re.compile(r"\|\|\s*true\b")
STAGE_ANNOUNCE_RE = re.compile(r'echo -n "  [^"]+?\.\.\. "')
RUN_ALL_CHECK_RE = re.compile(r"^\s*(?:python3|bash)\s+\"\$\{SCRIPT_DIR\}/check[_-][^\"]+\"")
QUIET_GREP_PIPE_RE = re.compile(r"\|\s*grep\s+(?:-[A-Za-z]*q[A-Za-z]*\b|[^#\n]*\s-q[A-Za-z]*\b)")

SAFE_COMMIT_REL = "scripts/safe-commit.sh"
RUN_ALL_REL = "scripts/checks/run-all.sh"

# Joining caps — a $( that never closes within this many physical lines
# stops accumulating (malformed script; shellcheck will scream anyway).
_MAX_JOIN_LINES = 50

REMEDIATION = """F83: shell gate script violates the gate-runner contract — the
#483 class where a gate dies silently under set -e and the log just
ends mid-stage with no FAIL line.

fix: per sub-rule —
  (a) unguarded VAR=$(cmd) under set -e: route the capture through an
      explicit guard so the exit code lands in a variable instead of
      tripping errexit: VAR=$(cmd) || RC=$? (or the run_gate helper
      pattern from scripts/safe-commit.sh). If the capture genuinely
      cannot fail, add # F83-allowed: <why> on the line.
  (b) bare `|| true`: append a trailing comment stating WHY ignoring
      the failure is correct, e.g. `|| true  # cleanup is best-effort`.
  (c) shellcheck errors: run `shellcheck --severity=error <file>` and
      fix what it reports.
  (d) stage-verdict ledger (safe-commit.sh / run-all.sh only): every
      stage announced with `echo -n "  <stage>... "` must emit both an
      OK${NC} success verdict and a FAIL${NC} failure verdict (or call
      gate_died); every run-all.sh check invocation must carry
      `|| overall=1`.
next: re-run python3 scripts/checks/check_f83_gate_runner_contract.py
to confirm the gate goes green. See #483 for the silent-death
post-mortem this rule mechanises.
run: bash scripts/safe-commit.sh "fix(scripts): guard command-substitution captures (#483 class)"

Pass example: scripts/safe-commit.sh (post-#483)
  GATE_RC=0
  run_gate() { GATE_RC=0; GATE_OUT=$("$@" 2>&1) || GATE_RC=$?; }
  echo -n "  mypy strict... "
  run_gate uv run mypy kairix/ --strict
  if [[ "$GATE_RC" -ne 0 ]]; then gate_died "mypy" "$GATE_RC" "..."; fi
  echo -e "${GREEN}OK${NC}"

Forbidden example:
  set -euo pipefail
  echo -n "  tests... "
  TEST_OUT=$(pytest tests/ 2>&1)   # pytest exits 1 -> script dies HERE.
  echo -e "${GREEN}OK${NC}"        # never reached; no FAIL line either —
                                   # the log just ends at "tests... "."""


def _logical_lines(source: str) -> list[tuple[int, str]]:
    """Join backslash continuations and unclosed ``$(...)`` spans.

    Returns ``(start_lineno, joined_text)`` per logical line. Paren
    balancing is naive (quotes are not tracked) — acceptable for the
    conservative assignment-shape detection this feeds.
    """
    physical = source.splitlines()
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(physical):
        start = i + 1
        buf = physical[i]
        joined = 0
        while buf.rstrip().endswith("\\") and i + 1 < len(physical) and joined < _MAX_JOIN_LINES:
            buf = buf.rstrip()[:-1] + " " + physical[i + 1]
            i += 1
            joined += 1
        while _has_unclosed_substitution(buf) and i + 1 < len(physical) and joined < _MAX_JOIN_LINES:
            buf = buf + " " + physical[i + 1]
            i += 1
            joined += 1
        out.append((start, buf))
        i += 1
    return out


def _has_unclosed_substitution(text: str) -> bool:
    """True iff a ``$(`` opens in ``text`` without its closing paren."""
    idx = text.find("$(")
    if idx == -1:
        return False
    depth = 0
    for ch in text[idx:]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                # One substitution closed; check for another opening later.
                rest_idx = text.find("$(", idx + 2)
                return False if rest_idx == -1 else _has_unclosed_substitution(text[rest_idx:])
    return depth > 0


def _unguarded_captures(source: str) -> list[str]:
    """Sub-rule (a): unguarded ``VAR=$(...)`` logical lines under set -e."""
    if not SET_E_RE.search(source):
        return []
    details: list[str] = []
    for lineno, text in _logical_lines(source):
        stripped = text.lstrip()
        if stripped.startswith("#"):
            continue
        if not ASSIGN_CAPTURE_RE.match(text):
            continue
        if EXEMPT_CAPTURE_RE.match(text):
            continue
        if RATIONALE_TAG in text or "||" in text:
            continue
        details.append(f"line {lineno}: unguarded capture under set -e — {stripped[:80]}")
    return details


def _bare_or_true(source: str) -> list[str]:
    """Sub-rule (b): ``|| true`` without a trailing comment on the line."""
    details: list[str] = []
    for lineno, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.lstrip()
        if stripped.startswith("#"):
            continue
        matches = list(OR_TRUE_RE.finditer(raw))
        if not matches:
            continue
        tail = raw[matches[-1].end() :]
        if "#" not in tail:
            details.append(f"line {lineno}: `|| true` without trailing rationale comment")
    return details


def _pipefail_unsafe_quiet_probes(source: str) -> list[str]:
    """Sub-rule (e): reject ``producer | grep -q`` under pipefail.

    A quiet grep exits as soon as it finds a match. With a sufficiently large
    producer payload, that closes the pipe while the producer is still writing;
    the resulting SIGPIPE makes the whole pipeline non-zero under ``pipefail``.
    Gate scripts must probe already-captured output through stdin redirection,
    which has only grep's truthful status: ``grep -q ... <<< "$OUTPUT"``.
    """
    if "pipefail" not in source:
        return []
    return [
        f"line {lineno}: quiet grep output probe can fail on upstream SIGPIPE under pipefail"
        for lineno, raw in enumerate(source.splitlines(), start=1)
        if not raw.lstrip().startswith("#") and QUIET_GREP_PIPE_RE.search(raw)
    ]


def _stage_ledger_contract(rel: str, source: str) -> list[str]:
    """Sub-rule (d): targeted stage-verdict assertions for the two runners."""
    details: list[str] = []
    if rel == SAFE_COMMIT_REL:
        announcements = list(STAGE_ANNOUNCE_RE.finditer(source))
        if not announcements:
            details.append('no `echo -n "  <stage>... "` announcements found — stage ledger convention missing')
        for idx, match in enumerate(announcements):
            seg_end = announcements[idx + 1].start() if idx + 1 < len(announcements) else len(source)
            segment = source[match.end() : seg_end]
            stage = match.group(0)
            if "OK${NC}" not in segment:
                details.append(f"stage {stage!r}: no OK${{NC}} success verdict in its block")
            if "FAIL${NC}" not in segment and "gate_died" not in segment:
                details.append(f"stage {stage!r}: no FAIL${{NC}} / gate_died failure verdict in its block")
    elif rel == RUN_ALL_REL:
        for lineno, raw in enumerate(source.splitlines(), start=1):
            if RUN_ALL_CHECK_RE.match(raw) and "|| overall=1" not in raw:
                details.append(f"line {lineno}: check invocation without `|| overall=1` ledger guard")
        if "passed" not in source or "FAILED" not in source:
            details.append("aggregate pass/FAILED verdict lines missing")
    return details


def _shellcheck_errors(repo_root: Path, files: list[Path]) -> dict[Path, list[str]]:
    """Sub-rule (c): shellcheck at error severity, one batched invocation.

    Returns ``{repo-relative-path: [detail, ...]}``. Empty when
    shellcheck is unavailable (notice printed by the caller).
    """
    if shutil.which("shellcheck") is None:
        return {}
    result = subprocess.run(
        ["shellcheck", "--severity=error", "--format=gcc", *[str(p) for p in files]],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    out: dict[Path, list[str]] = {}
    for raw in result.stdout.splitlines():
        parts = raw.split(":", 3)
        if len(parts) < 4:
            continue
        rel = Path(parts[0])
        out.setdefault(rel, []).append(f"shellcheck {parts[1]}:{parts[2]}:{parts[3].strip()[:100]}")
    return out


def _in_scope_files(repo_root: Path) -> list[Path]:
    """``scripts/*.sh`` + ``scripts/checks/*.sh``, repo-relative, sorted."""
    out: list[Path] = []
    for pattern in ("scripts/*.sh", "scripts/checks/*.sh"):
        out.extend(p.relative_to(repo_root) for p in repo_root.glob(pattern) if p.is_file())
    return sorted(set(out))


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Run all sub-rules; print per-file detail lines; return violating files."""
    files = _in_scope_files(repo_root)
    if shutil.which("shellcheck") is None:
        print("notice [arch:f83]: shellcheck not installed — sub-rule (c) skipped on this host (CI enforces it).")
    sc_errors = _shellcheck_errors(repo_root, files)

    violations: set[Path] = set()
    for rel in files:
        source = (repo_root / rel).read_text(encoding="utf-8")
        details = (
            _unguarded_captures(source)
            + _bare_or_true(source)
            + _pipefail_unsafe_quiet_probes(source)
            + _stage_ledger_contract(str(rel), source)
            + sc_errors.get(rel, [])
        )
        if details:
            violations.add(rel)
            for detail in details:
                print(f"  [f83] {rel}: {detail}")
    return violations


def main() -> int:
    return gate("f83", collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
