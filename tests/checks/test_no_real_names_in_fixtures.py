"""F32 detector tests — no real names in fixtures / corpora / docs.

The F32 detector (``scripts/checks/check_no_real_names_in_fixtures.py``)
flags identifiers in the curated ``REAL_NAMES`` set when they appear in
test fixtures (``tests/**/*.py``), BDD scenarios
(``tests/bdd/**/*.feature``), reference-library corpora
(``reference-library/**/*.{md,jsonl}``), or user-facing documentation
(``docs/**/*.md``).

Generic placeholders (``agent-alpha``, ``Acme``, ``Alice``/``Bob``/``Carol``)
must not false-positive. Files outside the F32 scope (e.g. a ``.py``
file under ``kairix/``) are not scanned by the detector orchestration —
the ``_scan_file`` unit covered here is the per-file primitive, so
scope is enforced one layer up in ``main()``.

Sabotage proof: blank out ``REAL_NAMES`` in the detector module to an
empty tuple, re-run these tests. The first two test cases flip red
because no name matches anymore. Restore. Executed during F32 landing:
mutate -> 3/4 tests fail -> restore -> 4/4 green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import check_no_real_names_in_fixtures as f32  # noqa: E402
from check_no_real_names_in_fixtures import (  # noqa: E402
    EXEMPT_FILES,
    EXEMPT_PATH_PREFIXES,
    _is_in_scope,
    _scan_file,
)

pytestmark = pytest.mark.unit


def _write_file(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_real_first_name_is_a_violation(tmp_path: Path) -> None:
    """A test fixture using a real first name (``Caroline``) trips the gate."""
    f = _write_file(
        tmp_path,
        "fixture.py",
        'record = FakeFactRecord(entity="Caroline", attribute="role", value="VP")\n',
    )

    violations = _scan_file(f, "tests/fixture.py")

    assert len(violations) == 1
    assert "Caroline" in violations[0]
    assert "tests/fixture.py:1" in violations[0]


def test_generic_placeholders_are_exempt(tmp_path: Path) -> None:
    """``agent-alpha`` / ``Acme`` / ``Alice``/``Bob``/``Carol`` must not trip
    the gate — they are the OK substitutes the rule pushes contributors
    toward, not the leak surface it polices.
    """
    f = _write_file(
        tmp_path,
        "fixture.py",
        (
            'record = FakeFactRecord(entity="agent-alpha", attribute="role", value="VP")\n'
            'transcript = "agent-beta works at Acme."\n'
            'crypto_canon = "Alice sends Bob a message; Carol observes."\n'
            'org = "your-team / your-org / Example Corp"\n'
        ),
    )

    violations = _scan_file(f, "tests/fixture.py")

    assert violations == []


def test_extension_outside_scope_is_skipped_by_orchestrator() -> None:
    """``_is_in_scope`` is the path-level gate. A ``.py`` under ``kairix/``
    is out of F32's scope even if its content would otherwise match —
    F32 only audits fixtures + corpora + docs, not production code.
    """
    # In-scope cases
    assert _is_in_scope("tests/use_cases/test_prep.py") is True
    assert _is_in_scope("tests/bdd/features/sample.feature") is True
    assert _is_in_scope("reference-library/conversations/session.jsonl") is True
    assert _is_in_scope("reference-library/agentic-ai/notes.md") is True
    assert _is_in_scope("docs/architecture/fitness-functions.md") is True

    # Out-of-scope cases
    assert _is_in_scope("kairix/core/search/pipeline.py") is False
    assert _is_in_scope("scripts/checks/check_no_real_names_in_fixtures.py") is False
    assert _is_in_scope("tests/use_cases/data.txt") is False  # tests/ but wrong ext
    assert _is_in_scope("docs/architecture/ENGINEERING.rst") is False  # docs/ but wrong ext


def test_rule_documentation_file_is_exempt() -> None:
    """``docs/architecture/fitness-functions.md`` documents the F32 rule
    itself and reuses the same identifier in the F30 worked example.
    It is the source-of-truth for the rule; exempting it stops the
    dogfood trap where the doc-that-defines-the-rule trips the rule.

    Sabotage-proof: remove ``docs/architecture/fitness-functions.md``
    from ``EXEMPT_FILES`` and this assertion fails because the path
    no longer appears in the exempt set.
    """
    assert "docs/architecture/fitness-functions.md" in EXEMPT_FILES


def test_reference_library_tree_is_exempt() -> None:
    """``reference-library/`` is vendored upstream scholarly content.
    Names inside are accurate citations of the authors of those works,
    not kairix-authored fixtures. Excluding the whole tree keeps the
    rule honest about what it's actually policing.

    Sabotage-proof: remove ``reference-library/`` from
    ``EXEMPT_PATH_PREFIXES`` and this assertion fails.
    """
    assert "reference-library/" in EXEMPT_PATH_PREFIXES


def test_kairix_owned_test_file_is_still_flagged(tmp_path: Path) -> None:
    """Symmetry check — exemptions cover docs + vendored content only.
    A kairix-authored test file under ``tests/`` is still in scope and
    a real name in it still trips the gate. Without this, the exemption
    additions above would silently widen into the kairix tree.

    Sabotage-proof: extend ``EXEMPT_PATH_PREFIXES`` to include
    ``tests/`` and this assertion fails because the path-level scope
    is no longer enforced for kairix-owned tests.
    """
    rel = "tests/use_cases/test_some_fixture.py"
    # No prefix in EXEMPT_PATH_PREFIXES may match a tests/ path.
    assert not any(rel.startswith(prefix) for prefix in EXEMPT_PATH_PREFIXES)
    # And the path is still in scope for the detector.
    assert _is_in_scope(rel) is True

    # And a real name inside that file still produces a violation.
    f = tmp_path / "test_some_fixture.py"
    f.write_text(
        'record = FakeFactRecord(entity="Caroline", attribute="role", value="VP")\n',
        encoding="utf-8",
    )
    assert len(_scan_file(f, rel)) == 1


def test_multiple_violations_in_one_file_all_get_reported(tmp_path: Path) -> None:
    """Every offending line surfaces as its own violation — the agent gets
    the full surgery list in one pass, not one line per iteration.
    """
    f = _write_file(
        tmp_path,
        "leaky.py",
        (
            'one = "Caroline runs product."\n'
            'two = "agent-alpha is fine."\n'
            'three = "contact: McMahon"\n'
            'four = "Dan McMahon is the author."\n'
            'five = "github.com/danielmcmahon/kairix"\n'
        ),
    )

    violations = _scan_file(f, "tests/leaky.py")

    # Lines 1, 3, 4, 5 contain a real name; line 2 (agent-alpha) is exempt.
    assert len(violations) == 4
    assert "leaky.py:1" in violations[0]
    assert "Caroline" in violations[0]
    assert "leaky.py:3" in violations[1]
    assert "McMahon" in violations[1]
    assert "leaky.py:4" in violations[2]
    # Multi-word match should bind the full token, not just the surname.
    assert "Dan McMahon" in violations[2]
    assert "leaky.py:5" in violations[3]
    assert "danielmcmahon" in violations[3]


def _recreate_retired_baseline(root: Path, gate_file: str, entry: str) -> None:
    """Write a file at the retired ``.architecture/baseline/`` location naming
    ``entry`` — the shape that used to grandfather it. PLA-472: it must have
    no effect on the verdict."""
    baseline = root / ".architecture" / "baseline" / gate_file
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(f"{entry}\n", encoding="utf-8")


def test_violation_fails_even_with_a_recreated_baseline_file(tmp_path: Path) -> None:
    """PLA-472: no grandfathering. A fixture listed in a re-created
    ``no-real-names-in-fixtures-files.txt`` is still reported.

    Sabotage proof (executed): re-adding the ``if rel in baseline: continue``
    skip to ``collect_violations`` drops the hit and this test goes red;
    restored → green.
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_leak.py").write_text('NAME = "Caroline"\n', encoding="utf-8")
    _recreate_retired_baseline(tmp_path, "no-real-names-in-fixtures-files.txt", "tests/test_leak.py")

    violations = f32.collect_violations(tmp_path, ["tests/test_leak.py"])

    assert len(violations) == 1
    assert violations[0].startswith("tests/test_leak.py:1")
    assert not hasattr(f32, "_load_baseline")
