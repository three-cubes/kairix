"""F83 detector tests for truthful shell gate output parsing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_f83_gate_runner_contract import _pipefail_unsafe_quiet_probes  # noqa: E402

pytestmark = pytest.mark.unit


def test_quiet_grep_pipeline_is_unsafe_under_pipefail() -> None:
    source = """
set -euo pipefail
if echo "$TEST_OUT" | grep -qE "[0-9]+ passed"; then
    echo OK
fi
"""

    assert _pipefail_unsafe_quiet_probes(source) == [
        "line 3: quiet grep output probe can fail on upstream SIGPIPE under pipefail"
    ]


def test_here_string_quiet_grep_is_safe_under_pipefail() -> None:
    source = """
set -euo pipefail
if grep -qE "[0-9]+ passed" <<< "$TEST_OUT"; then
    echo OK
fi
"""

    assert _pipefail_unsafe_quiet_probes(source) == []


@pytest.mark.parametrize("script", ["safe-commit.sh", "preflight.sh"])
def test_canonical_commit_gates_have_no_unsafe_quiet_output_probes(script: str) -> None:
    source = (_REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")

    assert _pipefail_unsafe_quiet_probes(source) == []
