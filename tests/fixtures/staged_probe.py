"""Probe-file harness for the staged-selection fitness tests.

Shared by ``tests/checks/test_staged_selection.py`` (the fast single-rule and
selection proofs) and ``tests/checks/test_staged_dispatch_smoke.py`` (the
whole-gate end-to-end dispatch smokes, integration tier). Probe files are
written under the real repo tree with a unique ``zzz_staged_probe`` name and
always removed on exit; :func:`purge_probe_debris` sweeps any an interrupt
orphaned (#504).
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_DIR = REPO_ROOT / "scripts" / "checks"
if str(CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(CHECKS_DIR))

import pytest  # noqa: E402
import run_checks  # noqa: E402
from run_checks import decide  # noqa: E402
from tc_fitness.context import CheckContext  # noqa: E402
from tc_fitness.staged import restrict_python_files  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def sweep_staged_probes() -> Iterator[None]:
    """Remove any ``zzz_staged_probe_*`` debris a prior INTERRUPTED run left in
    the repo tree, before AND after this session.

    A staged-selection probe that an interrupt orphaned (a hard ``Ctrl-C``
    between ``write_text`` and the ``finally`` unlink) used to leave a
    ``zzz_staged_probe_*.py`` under ``tests/`` or ``kairix/`` that a later
    full-tree scan would pick up — the #504 isolation flake. The single-rule
    narrowing (:func:`run_one_narrowed`) already makes the F8/F26 scans
    structurally immune (they never walk the whole tree), and this fixture is
    the belt-and-braces complement: it makes the WHOLE cluster idempotent under
    interrupt by sweeping every orphaned probe file and probe directory at
    session boundaries. Only ever touches uniquely-named ``zzz_staged_probe*``
    paths, so it can never delete a real file."""
    purge_probe_debris()
    try:
        yield
    finally:
        purge_probe_debris()


def purge_probe_debris() -> None:
    """Delete every ``zzz_staged_probe*`` file or directory under the repo tree
    (the uniquely-named probe namespace — never a real path)."""
    import shutil

    for path in sorted(REPO_ROOT.rglob("zzz_staged_probe*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


@contextlib.contextmanager
def probe_file(rel: str, content: str) -> Iterator[str]:
    """Write ``content`` to ``rel`` under the real repo tree, yield the
    repo-relative path string, and FULLY remove it on exit — the file, any
    ``__pycache__`` an import created, and any directory the probe itself
    created (try/finally so a failed assert never leaves a shadow). ``rel``
    must be a ``zzz_staged_probe*`` path so it can never collide with a real
    file, and the probe-dir teardown only deletes ``zzz_staged_probe*``
    directories so it can never touch a real tree."""
    assert "zzz_staged_probe" in rel, "probe paths must be uniquely named to avoid collisions"
    path = REPO_ROOT / rel
    created_dir = "zzz_staged_probe" in path.parent.name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    try:
        yield rel
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        # A staged-mode dispatch may have imported the probe, leaving a
        # __pycache__ that would keep a probe PLUGIN dir alive (F36/F41/...
        # treat any dir under kairix/connectors/ as a plugin). Remove the
        # whole probe dir — but only ever a uniquely-named probe dir.
        if created_dir and path.parent.exists():
            import shutil

            shutil.rmtree(path.parent, ignore_errors=True)


def run_staged(staged: list[str]) -> tuple[int, str]:
    """Drive the real ``_dispatch_staged`` over ``staged``; return
    ``(exit_code, captured_output)``.

    This runs the FULL ~20-50-rule staged gate — it is reserved for the ONE
    retained end-to-end smoke that proves the dispatch wiring is intact. The
    single-rule proofs use :func:`run_one_narrowed` instead (one rule, narrowed
    to the staged probe) so they neither pay the whole-gate cost nor walk the
    whole tree (#504)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = run_checks._dispatch_staged(staged, skip_coverage=True)
    return code, buf.getvalue()


def run_one_narrowed(rule_id: str, staged: list[str]) -> tuple[int, str]:
    """Dispatch ONLY ``rule_id`` over ``staged`` through the REAL staged path,
    narrowed to the decision's staged files — the single-rule equivalent of the
    runner's ``_run_staged_one``.

    Drives kairix's real ``decide`` to get the rule's :class:`StagedDecision`,
    then runs that one rule in-process inside ``restrict_python_files`` +
    kairix's ``_enumeration_narrower`` scoped to ``decision.scope_files`` — so a
    file-local detector walks ONLY the staged probe, exactly as the full staged
    dispatch would scope it. This is the same code path ``_run_staged_one`` takes
    for a file-local rule, isolated to one rule so a single-rule proof costs
    <0.2s instead of re-running the whole gate (and never walks the full tree,
    closing the #504 stale-probe sensitivity). The decision MUST be ``run`` —
    these proofs stage a path the rule's scope contains.

    Returns ``(rc, captured_output)`` where ``rc`` is 0 (pass) / 1 (fail)."""
    entry = next(e for e in run_checks._select_all() if e.id == rule_id)
    script = run_checks.resolve_script(entry)
    decision = decide(entry, script, staged)
    assert decision.run, f"{rule_id} must be selected for staged={staged}; reason: {decision.reason}"
    buf = io.StringIO()
    ctx = CheckContext(repo_root=run_checks.REPO_ROOT)
    with ctx.install(), contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        with contextlib.ExitStack() as stack:
            if decision.scope_files:
                scope_files = list(decision.scope_files)
                stack.enter_context(restrict_python_files(run_checks.REPO_ROOT, scope_files))
                stack.enter_context(run_checks._enumeration_narrower(run_checks.REPO_ROOT, scope_files))
            rc = run_checks._run_one_inprocess(entry, ctx)
    return rc, buf.getvalue()


def failed_rule_ids(output: str) -> set[str]:
    """The rule ids that FAILED in a staged ledger (the ``FAIL [id]`` lines)."""
    ids: set[str] = set()
    for line in output.splitlines():
        if "FAIL [" in line:
            ids.add(line.split("FAIL [", 1)[1].split("]", 1)[0])
    return ids


def ran_rule_ids(output: str) -> set[str]:
    """The rule ids that RAN (the ``run [id]`` lines) in a staged ledger."""
    ids: set[str] = set()
    for line in output.splitlines():
        if "run [" in line:
            ids.add(line.split("run [", 1)[1].split("]", 1)[0])
    return ids


def skipped_rule_ids(output: str) -> set[str]:
    ids: set[str] = set()
    for line in output.splitlines():
        if "skip [" in line:
            ids.add(line.split("skip [", 1)[1].split("]", 1)[0])
    return ids
