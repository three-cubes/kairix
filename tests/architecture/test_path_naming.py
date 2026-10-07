"""Unit tests for F22 (``scripts/checks/check_path_naming.py``).

F22 enforces per-tree filename conventions: ``kairix/**/*.py`` →
snake_case, ``tests/**/test_*.py``, ``tests/bdd/features/*.feature``
→ snake_case, ``scripts/checks/check_*.{py,sh}``, ``docs/**/runbooks/*.md``
→ kebab-case. There is no baseline tree any more (PLA-472).

Each test has a paired sabotage-proof: flip the path to a non-
conforming shape and confirm the detector now fires (and vice versa).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_path_naming.py"


def _load_detector():
    """Load the F22 detector module by file path (it lives outside
    the kairix package so we can't ``import`` it normally).
    """
    spec = importlib.util.spec_from_file_location("_f22_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f22_detector"] = module
    spec.loader.exec_module(module)
    return module


def test_snake_case_python_module_passes() -> None:
    """A ``snake_case.py`` module under ``kairix/`` satisfies the rule.

    Sabotage-proof: the PascalCase variant is flagged (see
    ``test_pascal_case_python_module_fails``).
    """
    detector = _load_detector()
    assert detector.file_violates("kairix/core/search/pipeline.py") is False


def test_pascal_case_python_module_fails() -> None:
    """A PascalCase ``.py`` under ``kairix/`` is flagged.

    Sabotage-proof inline: renaming to snake_case clears the flag.
    """
    detector = _load_detector()
    assert detector.file_violates("kairix/core/SearchPipeline.py") is True
    assert detector.file_violates("kairix/core/search_pipeline.py") is False


def test_leading_underscore_private_module_passes() -> None:
    """Python convention allows ``_private.py``; F22 must permit it.

    Sabotage-proof: a leading-uppercase variant (``_Foo.py``) still
    fails.
    """
    detector = _load_detector()
    assert detector.file_violates("kairix/providers/_base.py") is False
    assert detector.file_violates("kairix/core/_internal.py") is False
    assert detector.file_violates("kairix/core/_Foo.py") is True


def test_test_module_naming() -> None:
    """``test_*.py`` passes; non-test_-prefixed PascalCase fails.

    Sabotage-proof inline: snake_case helper modules pass; PascalCase
    test files fail.
    """
    detector = _load_detector()
    assert detector.file_violates("tests/search/test_pipeline.py") is False
    assert detector.file_violates("tests/fixtures/embeddings.py") is False  # helper
    assert detector.file_violates("tests/search/PipelineTest.py") is True


def test_bdd_feature_naming() -> None:
    """BDD ``.feature`` files must be snake_case.

    Sabotage-proof inline: PascalCase / CamelCase features fail.
    """
    detector = _load_detector()
    assert detector.file_violates("tests/bdd/features/search_returns_hits.feature") is False
    assert detector.file_violates("tests/bdd/features/SearchReturnsHits.feature") is True


def test_check_script_naming() -> None:
    """``scripts/checks/check_*.py`` and ``check-*.sh`` pass; arbitrary
    names fail.

    Sabotage-proof inline: ``CheckFoo.py`` flagged; ``check_foo.py``
    clears.
    """
    detector = _load_detector()
    assert detector.file_violates("scripts/checks/check_path_naming.py") is False
    assert detector.file_violates("scripts/checks/_fitness_rule.py") is False
    assert detector.file_violates("scripts/checks/run-all.sh") is False
    assert detector.file_violates("scripts/checks/CheckPathNaming.py") is True


def test_runbook_naming() -> None:
    """Runbooks must be kebab-case, ``INDEX.md``, or ``README.md``.

    ``README.md`` is allowed as the per-directory resolver (F23) — landing
    on ``docs/runbooks/`` from a path mention hits the README first.

    Sabotage-proof inline: snake_case runbook fails; kebab-case clears;
    INDEX/README clear.
    """
    detector = _load_detector()
    assert detector.file_violates("docs/operations/runbooks/how-to-debug-search-ranking.md") is False
    assert detector.file_violates("docs/operations/runbooks/INDEX.md") is False
    assert detector.file_violates("docs/runbooks/README.md") is False
    assert detector.file_violates("docs/runbooks/my_runbook.md") is True
    assert detector.file_violates("docs/runbooks/my-runbook.md") is False


def test_retired_baseline_tooling_is_not_a_sanctioned_check_script_name() -> None:
    """PLA-472 deleted ``audit_baselines.py`` and the ``.architecture/baseline/``
    tree. F22 no longer sanctions either: a re-created ``audit_baselines.py``
    under ``scripts/checks/`` is a naming violation, and the baseline tree
    has no tree-rule of its own.

    Sabotage proof (executed): re-adding ``audit_baselines`` to
    ``_CHECK_SCRIPT_PY`` flips the first assertion red; re-adding the
    ``.architecture/baseline/`` tree-rule flips the second. Restored → green.
    """
    detector = _load_detector()
    assert detector.file_violates("scripts/checks/audit_baselines.py") is True
    assert not any(prefix.startswith(".architecture") for prefix, _, _ in detector._TREE_RULES)


def test_out_of_scope_paths_pass_silently() -> None:
    """A file outside every registered tree (``docker/``,
    ``reference-library/``, top-level config) is not constrained.

    Sabotage-proof: F22 deliberately doesn't claim the whole repo.
    """
    detector = _load_detector()
    assert detector.file_violates("docker/entrypoint.sh") is False
    assert detector.file_violates("reference-library/Something-Weird.md") is False
    assert detector.file_violates("README.md") is False
    assert detector.file_violates("Dockerfile") is False


def test_real_repo_path_naming_gate_is_green() -> None:
    """The real ``scripts/checks/check_path_naming.py`` run against the
    full repo emits no violations (there is no grandfathering).

    Sabotage-proof: the unit-level cases above prove the detector
    fires on bad shapes; this case proves the *real run* is green.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F22's own REMEDIATION must satisfy F21 — the agent reading a
    path-naming failure should get the correction action inline.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
