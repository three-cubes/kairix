"""F13: BDD feature files must not reference implementation symbols.

Per Dan North (BDD), Gojko Adzic (Specification by Example), and Liz
Keogh, BDD scenarios describe *stakeholder outcomes*, not the code
that implements them. A scenario that mentions ``Mock``, ``pytest``,
or ``kairix.core.search.bm25`` is not a specification — it's a unit
test masquerading as one. The smell is "scenario describes internals."

Forbidden tokens (any occurrence in a non-comment line):

  - Test-framework leakage:
      ``Mock`` | ``MagicMock`` | ``monkeypatch`` | ``pytest.`` | ``unittest.``

  - Internal module paths (``kairix.<package>.<symbol>``), excluding
    well-known config-file names like ``kairix.config.yaml`` and
    ``kairix.paths.yaml``.

F13 does NOT (yet) catch:

  - Soft leaks in prose ("the code", "the function does X")
  - Generic exception names ("Exception", "Error") — these are nouns
    the user sometimes sees, not always internals
  - Abstraction-level review (whether the scenario describes a
    business outcome at all). That's a Three Amigos / human-review
    concern; see the aspirational issue.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import REPO_ROOT, gate, repo_relative

REMEDIATION = """A BDD scenario should describe what a stakeholder sees, not
how the code is implemented. Forbidden tokens detected (Mock / MagicMock /
monkeypatch / pytest. / unittest. / kairix.<pkg>.<symbol>) are
implementation symbols.

Rewrite in stakeholder language:
  Bad:  Then a Mock is returned
  Good: Then the operator sees the cached suite

  Bad:  When kairix.core.search.bm25.bm25_search runs
  Good: When the operator runs a search

If the test is genuinely about internals, it does not belong in
tests/bdd/features/ — move it to a unit test. See Dan North on BDD
scenarios as living documentation, and Liz Keogh on "test infection
of BDD"."""


# File-extension allowlist: ``kairix.X.yaml`` is a config-file name,
# not a module path. Add new extensions here if they leak through.
_FILE_EXTS = ("yaml", "yml", "json", "toml", "py", "md", "txt", "xml", "lock", "feature")

_FILE_EXT_RE = re.compile(rf"^({'|'.join(_FILE_EXTS)})\b")

# Test-framework leakage patterns.
_FRAMEWORK_PATTERNS = [
    re.compile(r"\bMock\b"),
    re.compile(r"\bMagicMock\b"),
    re.compile(r"\bmonkeypatch\b"),
    re.compile(r"\bpytest\."),
    re.compile(r"\bunittest\."),
]

# Internal module-path pattern: kairix.<pkg>.<symbol>. Anchored to lowercase
# segments since module names are always lowercase. We exclude matches whose
# third segment is a known file extension.
_MODULE_PATH_RE = re.compile(r"\bkairix\.[a-z_]+\.([a-z_]+)\b")


def _line_has_violation(line: str) -> bool:
    """True if the line contains a forbidden implementation symbol."""
    # Skip Gherkin comment lines (start with `#` after whitespace)
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False

    for pat in _FRAMEWORK_PATTERNS:
        if pat.search(line):
            return True

    # For each kairix.X.Y match, fail unless Y is a known file extension.
    for match in _MODULE_PATH_RE.finditer(line):
        third_segment = match.group(1)
        # Look ahead in the original string after the match — if the
        # captured third segment IS a file extension, accept it.
        if _FILE_EXT_RE.match(third_segment):
            continue
        return True

    return False


def file_has_violation(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return any(_line_has_violation(line) for line in text.splitlines())


def main() -> int:
    features_dir = REPO_ROOT / "tests" / "bdd" / "features"
    if not features_dir.exists():
        return gate("bdd-no-implementation-leaks", set(), REMEDIATION)
    violations = {repo_relative(p) for p in features_dir.rglob("*.feature") if file_has_violation(p)}
    return gate("bdd-no-implementation-leaks", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
