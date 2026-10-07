"""Sonar zero-open-findings gate tests (deterministic, no live Sonar).

The gate (``scripts/checks/check_sonar_new_code.py``) fails on ANY open
SonarCloud issue or hotspot whose flagged code is still present in the working
copy — there is no committed baseline. An issue counts as resolved locally
once no line of its file carries Sonar's line hash (MD5 of the line with all
whitespace removed); a hotspot has no hash, so it stays present until its file
is gone.

These tests inject fake findings and a tmp repo root into the pure verdict core
(``evaluate`` / ``present_locally`` / ``sonar_line_hash``); they never touch the
network. Each test carries a sabotage proof — the executed
mutate -> fail -> restore that proves the assertion is load-bearing.

Sabotage proofs (executed against scripts/checks/check_sonar_new_code.py):
  - test_issue_on_unchanged_line_fails: make ``present_locally`` return False
    when the hash matches -> the finding is dropped and this test goes red.
    Restored.
  - test_issue_resolved_when_flagged_line_changed: make ``present_locally``
    return True unconditionally -> the fixed finding is still reported and this
    test goes red. Restored.
  - test_line_hash_ignores_whitespace: drop the whitespace strip in
    ``sonar_line_hash`` -> the re-indented line no longer matches and this
    test goes red. Restored.
  - test_hotspot_without_hash_stays_present: return False when ``line_hash``
    is None -> the hotspot is dropped and this test goes red. Restored.
  - test_working_set_scope_excludes_unchanged_files: ignore ``files_in_scope``
    in ``evaluate`` -> the out-of-scope finding is reported and this test goes
    red. Restored.
  - test_fix_next_to_the_anchor_line_resolves_the_issue: set ``CONTEXT_LINES``
    to 0 -> the edit two lines below the anchor no longer counts and this test
    goes red. Restored.
  - test_untouched_region_stays_present_even_when_file_changed_elsewhere:
    return False once the file differs at all from ``base_text`` -> the far
    edit wrongly resolves the issue and this test goes red. Restored.
  - test_flow_location_change_resolves_the_issue: drop the flow locations in
    ``_location_lines`` -> the fixed taint source no longer counts and this
    test goes red. Restored.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_sonar_new_code import (  # noqa: E402
    Finding,
    _location_lines,
    evaluate,
    present_locally,
    sonar_line_hash,
)

pytestmark = pytest.mark.unit

_FLAGGED = 'PATTERN = re.compile(r"(a+)+$")'


def _issue(path: str, line_text: str, line: int = 1) -> Finding:
    return Finding(
        path=path,
        line=line,
        rule="python:S8786",
        message="Simplify this regular expression",
        kind="issue",
        line_hash=sonar_line_hash(line_text),
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_line_hash_matches_sonar_algorithm() -> None:
    """Sonar's line hash is MD5 of the whitespace-stripped line."""
    expected = hashlib.md5(b'PATTERN=re.compile(r"(a+)+$")', usedforsecurity=False).hexdigest()

    assert sonar_line_hash(_FLAGGED) == expected


def test_line_hash_ignores_whitespace() -> None:
    """Re-indenting a flagged line does not resolve the finding."""
    assert sonar_line_hash("    " + _FLAGGED + "\t") == sonar_line_hash(_FLAGGED)


def test_issue_on_unchanged_line_fails(tmp_path: Path) -> None:
    """An open issue whose flagged line is still in the file is reported."""
    _write(tmp_path, "kairix/x.py", f"import re\n{_FLAGGED}\n")
    finding = _issue("kairix/x.py", _FLAGGED, line=2)

    assert evaluate([finding], files_in_scope=None, repo_root=tmp_path) == [finding]


def test_issue_resolved_when_flagged_line_changed(tmp_path: Path) -> None:
    """Rewriting the flagged line resolves the issue locally (before re-scan)."""
    _write(tmp_path, "kairix/x.py", 'import re\nPATTERN = re.compile(r"a+$")\n')
    finding = _issue("kairix/x.py", _FLAGGED, line=2)

    assert evaluate([finding], files_in_scope=None, repo_root=tmp_path) == []


def test_issue_in_deleted_file_is_resolved(tmp_path: Path) -> None:
    """A finding in a file that no longer exists is not present."""
    assert present_locally(_issue("kairix/gone.py", _FLAGGED), tmp_path) is False


def test_hotspot_without_hash_stays_present(tmp_path: Path) -> None:
    """A hotspot carries no line hash, so it is present while its file exists."""
    _write(tmp_path, "kairix/y.py", "x = 1\n")
    hotspot = Finding(path="kairix/y.py", line=1, rule="S5332", message="http", kind="hotspot")

    assert evaluate([hotspot], files_in_scope=None, repo_root=tmp_path) == [hotspot]


def test_working_set_scope_excludes_unchanged_files(tmp_path: Path) -> None:
    """Working-set mode only gates files in this change."""
    _write(tmp_path, "kairix/x.py", f"{_FLAGGED}\n")
    _write(tmp_path, "kairix/other.py", f"{_FLAGGED}\n")
    in_scope = _issue("kairix/x.py", _FLAGGED)
    out_of_scope = _issue("kairix/other.py", _FLAGGED)

    remaining = evaluate([in_scope, out_of_scope], files_in_scope={"kairix/x.py"}, repo_root=tmp_path)

    assert remaining == [in_scope]


# ── region semantics (analysed revision available) ──────────────────────

_BASE = "\n".join(f"line {n}" for n in range(1, 21)) + "\n"


def _anchored(lines: tuple[int, ...]) -> Finding:
    return Finding(
        path="kairix/r.py",
        line=lines[0],
        rule="python:S8786",
        message="m",
        kind="issue",
        line_hash=sonar_line_hash(f"line {lines[0]}"),
        lines=lines,
    )


def test_fix_next_to_the_anchor_line_resolves_the_issue(tmp_path: Path) -> None:
    """Sonar anchors on ``re.compile(`` while the regex body sits below it;
    editing a line within the context window resolves the finding locally."""
    _write(tmp_path, "kairix/r.py", _BASE.replace("line 12\n", "line 12 (linear)\n"))

    assert evaluate([_anchored((10,))], None, tmp_path, lambda _p: _BASE) == []


def test_untouched_region_stays_present_even_when_file_changed_elsewhere(tmp_path: Path) -> None:
    """An unrelated edit far from the finding does not resolve it."""
    _write(tmp_path, "kairix/r.py", _BASE.replace("line 19\n", "line 19 (edited)\n"))
    finding = _anchored((5,))

    assert evaluate([finding], None, tmp_path, lambda _p: _BASE) == [finding]


def test_flow_location_change_resolves_the_issue(tmp_path: Path) -> None:
    """A taint-flow finding is resolved by changing a secondary (flow) location
    far from its primary line, e.g. where the untrusted value enters."""
    _write(tmp_path, "kairix/r.py", _BASE.replace("line 18\n", "line 18 (validated)\n"))
    issue = {
        "textRange": {"startLine": 4, "endLine": 4},
        "flows": [{"locations": [{"component": "three-cubes_kairix:kairix/r.py", "textRange": {"startLine": 18}}]}],
    }

    lines = _location_lines(issue, "kairix/r.py")

    assert lines == (4, 18)
    assert present_locally(_anchored(lines), tmp_path, _BASE) is False


def test_location_lines_ignore_other_files_flows() -> None:
    """Flow locations in another file don't widen this file's region."""
    issue = {
        "textRange": {"startLine": 68, "endLine": 70},
        "flows": [{"locations": [{"component": "three-cubes_kairix:kairix/other.py", "textRange": {"startLine": 3}}]}],
    }

    assert _location_lines(issue, "kairix/r.py") == (68, 69, 70)
