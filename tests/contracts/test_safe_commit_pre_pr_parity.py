"""Behavioural contract for the local pre-push CI parity path."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.contract


def test_pre_pr_prepares_inputs_then_runs_each_ci_tier_once(tmp_path: Path) -> None:
    """The public ``--pre-pr`` command prepares before both CI tiers."""
    repo_root = Path(__file__).resolve().parents[2]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "uv-calls.log"
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$*" >> "$UV_CALL_LOG"\n'
        'if [[ "${1:-}" == "run" && "${2:-}" == "pytest" ]]; then\n'
        "  printf '1 passed in 0.01s\\n'\n"
        "fi\n"
    )
    uv.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "UV_CALL_LOG": str(call_log),
    }
    result = subprocess.run(
        ["bash", "scripts/safe-commit.sh", "--pre-pr"],
        cwd=repo_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = call_log.read_text().splitlines()
    assert calls[0].startswith("sync --locked ")
    selected_calls = [line for line in calls if line.startswith("run python ") or line.startswith("run pytest ")]
    assert selected_calls == [
        "run python scripts/checks/generate_catalogue_docs.py",
        "run python -m kairix.agents.usage_guide.generate",
        "run pytest tests/ -m integration --maxfail=3",
        "run pytest -m e2e tests/e2e/ -v --tb=short",
    ]
    assert "CI Stage 3 integration and Stage 4.5 composed E2E are green" in result.stdout
