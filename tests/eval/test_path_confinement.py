"""Path-confinement helper tests (#143 Phase 0b).

``confine_to(root, candidate)`` is the helper that any new eval / judge code
must use when an external input (CLI flag, YAML field, env var) drives a
filesystem path. The helper resolves the candidate against the allowed root
and verifies it stays inside, raising :class:`PathTraversalError` on escape.

These tests sabotage-prove the helper itself — the canonical
``../../../etc/passwd`` traversal payload is the headline case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.quality.eval.security import (
    PathTraversalError,
    confine_to,
)


@pytest.mark.unit
def test_confine_to_accepts_relative_path_inside_root(tmp_path: Path) -> None:
    """A relative path that lives inside the root resolves cleanly."""
    (tmp_path / "suites").mkdir()
    (tmp_path / "suites" / "canary.yaml").write_text("cases: []", encoding="utf-8")
    resolved = confine_to(tmp_path, "suites/canary.yaml")
    assert resolved == (tmp_path / "suites" / "canary.yaml").resolve()


@pytest.mark.unit
def test_confine_to_rejects_dotdot_traversal(tmp_path: Path) -> None:
    """``../../../etc/passwd`` payload must be rejected with PathTraversalError."""
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    with pytest.raises(PathTraversalError) as excinfo:
        confine_to(eval_root, "../../../etc/passwd")
    assert "escapes allowed root" in str(excinfo.value)


@pytest.mark.unit
def test_confine_to_rejects_absolute_path_outside_root(tmp_path: Path) -> None:
    """An absolute path pointing outside the allowed root must be rejected."""
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    outside = tmp_path / "secrets" / "creds.yaml"
    with pytest.raises(PathTraversalError):
        confine_to(eval_root, outside)


@pytest.mark.unit
def test_confine_to_accepts_absolute_path_inside_root(tmp_path: Path) -> None:
    """An absolute path that already lives under the root resolves cleanly."""
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    inside = eval_root / "results.json"
    resolved = confine_to(eval_root, inside)
    assert resolved == inside.resolve()


@pytest.mark.unit
def test_confine_to_raises_value_error_for_legacy_callers(tmp_path: Path) -> None:
    """``PathTraversalError`` is a ``ValueError`` subclass so legacy
    ``except ValueError`` blocks catch it without code churn."""
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    with pytest.raises(ValueError):
        confine_to(eval_root, "../../../etc/passwd")


@pytest.mark.unit
def test_confine_to_rejects_symlink_pointing_outside(tmp_path: Path) -> None:
    """A symlink inside the root that targets a path outside must be rejected.

    ``Path.resolve()`` follows symlinks, so the escape check runs on the
    real target, not the link path.
    """
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    outside_target = tmp_path / "secret.txt"
    outside_target.write_text("oops", encoding="utf-8")
    link = eval_root / "shortcut.yaml"
    try:
        link.symlink_to(outside_target)
    except (OSError, NotImplementedError):  # pragma: no cover — symlink unsupported on this platform
        pytest.skip("symlink creation not supported in this environment")
    with pytest.raises(PathTraversalError):
        confine_to(eval_root, link)
