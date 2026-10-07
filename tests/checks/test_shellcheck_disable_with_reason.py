"""F33 detector tests -- shellcheck disable directives require rationale.

The F33 detector (``scripts/checks/check_shellcheck_disable_with_reason.py``)
flags ``# shellcheck disable=<rule>`` lines whose neither the same line
nor the immediately preceding ``#``-comment line carries a rationale.

The tests below pin the detector's behaviour on the four cases that
matter:

- A bare disable with nothing after it -- violation.
- A disable with an inline rationale comment after it -- exempt.
- A disable whose preceding line is a substantive ``#`` comment -- exempt.
- A multi-rule disable (``disable=SC2034,SC2046``) with no rationale --
  violation.

Sabotage proof for the rationale path: mutate the
``_trailing_has_rationale`` helper to always return ``False`` and the
two exempt cases below flip red (still requires the preceding-line
helper to also flip false for full exemption coverage; the inline-only
case is enough to catch the sabotage).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

import check_shellcheck_disable_with_reason as f33  # noqa: E402
from check_shellcheck_disable_with_reason import _scan_file  # noqa: E402

pytestmark = pytest.mark.unit


def _write_file(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


def test_bare_disable_is_a_violation(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "bare.sh",
        '#!/usr/bin/env bash\n# shellcheck disable=SC1090\n. "$CFG"\n',
    )

    violations = _scan_file(f, "bare.sh")

    assert len(violations) == 1
    assert "shellcheck disable without rationale" in violations[0]
    assert "bare.sh:2" in violations[0]


def test_inline_rationale_is_exempt(tmp_path: Path) -> None:
    content = (
        "#!/usr/bin/env bash\n"
        "# shellcheck disable=SC1090  # safe -- path is computed from a vetted config var\n"
        '. "$CFG"\n'
    )
    f = _write_file(tmp_path, "inline.sh", content)

    violations = _scan_file(f, "inline.sh")

    assert violations == []


def test_preceding_comment_is_exempt(tmp_path: Path) -> None:
    content = (
        "#!/usr/bin/env bash\n"
        "# why: path resolved at runtime; shellcheck cannot statically prove it\n"
        "# shellcheck disable=SC1090\n"
        '. "$CFG"\n'
    )
    f = _write_file(tmp_path, "preceding.sh", content)

    violations = _scan_file(f, "preceding.sh")

    assert violations == []


def test_multi_rule_disable_without_rationale_is_a_violation(tmp_path: Path) -> None:
    f = _write_file(
        tmp_path,
        "multi.sh",
        "#!/usr/bin/env bash\n# shellcheck disable=SC2034,SC2046\nx=1\n",
    )

    violations = _scan_file(f, "multi.sh")

    assert len(violations) == 1
    assert "multi.sh:2" in violations[0]


def test_short_preceding_comment_does_not_count_as_rationale(tmp_path: Path) -> None:
    """A stub ``# ok`` line above the disable is not a rationale."""
    f = _write_file(
        tmp_path,
        "stub.sh",
        "#!/usr/bin/env bash\n# ok\n# shellcheck disable=SC2034\nx=1\n",
    )

    violations = _scan_file(f, "stub.sh")

    assert len(violations) == 1
    assert "stub.sh:3" in violations[0]


def _recreate_retired_baseline(root: Path, gate_file: str, entry: str) -> None:
    """Write a file at the retired ``.architecture/baseline/`` location naming
    ``entry`` — the shape that used to grandfather it. PLA-472: it must have
    no effect on the verdict."""
    baseline = root / ".architecture" / "baseline" / gate_file
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_text(f"{entry}\n", encoding="utf-8")


def test_violation_fails_even_with_a_recreated_baseline_file(tmp_path: Path) -> None:
    """PLA-472: no grandfathering. A bare disable in a file listed in a
    re-created ``shellcheck-disable-with-reason-files.txt`` is still reported.

    Sabotage proof (executed): re-adding the ``if rel in baseline: continue``
    skip to ``collect_violations`` drops the hit and this test goes red;
    restored → green.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run.sh").write_text(
        '#!/usr/bin/env bash\n# shellcheck disable=SC1090\n. "$X"\n', encoding="utf-8"
    )
    _recreate_retired_baseline(tmp_path, "shellcheck-disable-with-reason-files.txt", "scripts/run.sh")

    violations = f33.collect_violations(tmp_path, ["scripts/run.sh"])

    assert violations == ["scripts/run.sh:2: shellcheck disable without rationale"]
    assert not hasattr(f33, "_load_baseline")
    assert f33._is_exempt_path(".architecture/baseline/run.sh") is False
