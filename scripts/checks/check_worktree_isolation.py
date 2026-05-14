"""Defensive check for #208 — agent worktree-isolation leakage.

Tracks the failure mode reported in issue #208 (kairix) + upstream
``anthropics/claude-code#59019``: a subagent dispatched with
``Agent(isolation="worktree", run_in_background=True)`` should write
files only inside its assigned worktree directory under
``.claude/worktrees/agent-<id>/``. In practice, subagents sometimes
write into the **parent** (primary) checkout's working tree as well,
leaving untracked files that surprise the orchestrator's next
``git status`` and break subsequent ``git cherry-pick`` operations.

This script audits the primary checkout for untracked files whose
path also exists inside any active subagent worktree. Hits are the
fingerprint of the leakage and should be cleaned up (the canonical
source of truth is the subagent's commit; the parent's untracked
copy is the stale shadow).

Usage:
    python3 scripts/checks/check_worktree_isolation.py            # report only
    python3 scripts/checks/check_worktree_isolation.py --clean    # delete leaked copies

Exit 0 if no leakage detected; exit 1 if leakage detected (so the
orchestrator can chain with ``&&``).

Not currently wired into ``run-all.sh`` — this is an ad-hoc
integrity check the orchestrator runs before cherry-picking a
subagent's commit. Wiring it into pre-commit would surface false
positives during interactive development where untracked files are
legitimate work-in-progress.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKTREES_DIR = REPO_ROOT / ".claude" / "worktrees"

REMEDIATION = """Refactor to remove the shadow untracked files in the
primary checkout (the subagent's commit is canonical; the primary's
copy is stale leakage from anthropics/claude-code#59019) — to pass.

fix: re-run this script with ``--clean`` to delete the primary's
copies, then continue with ``git cherry-pick <subagent-sha>`` — the
subagent's commit is the canonical version and will land cleanly once
the shadow copies are gone.
next: re-run ``python3 scripts/checks/check_worktree_isolation.py``
(no flags) to confirm the gate reports zero shadow hits.
run: python3 scripts/checks/check_worktree_isolation.py --clean

Pass example:
  $ python3 scripts/checks/check_worktree_isolation.py
  ok [worktree-isolation] — 3 untracked file(s) in primary, none also
    present in 2 active subagent worktree(s).

Forbidden example (untracked file shadows a subagent worktree path):
  $ python3 scripts/checks/check_worktree_isolation.py
  FAIL [worktree-isolation] — 1 leak fingerprint(s) detected:
    kairix/foo.py  (also exists in .claude/worktrees/agent-abc123)

The subagent that wrote the file is the source of truth; the primary
must not retain a parallel copy or the next cherry-pick will conflict."""


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else str(REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _untracked_in_primary() -> list[Path]:
    """Repo-relative paths that ``git`` reports untracked in the primary."""
    raw = _git("status", "--porcelain")
    out: list[Path] = []
    for line in raw.splitlines():
        if not line:
            continue
        # Porcelain '??' = untracked. Two-char status, then space, then path.
        if line.startswith("?? "):
            out.append(Path(line[3:]))
    return out


def _active_worktrees() -> list[Path]:
    """List ``.claude/worktrees/agent-*`` directories that currently exist."""
    if not WORKTREES_DIR.exists():
        return []
    return [p for p in WORKTREES_DIR.iterdir() if p.is_dir() and p.name.startswith("agent-")]


def _shadow_hits(untracked: list[Path], worktrees: list[Path]) -> list[tuple[Path, Path]]:
    """For each untracked-in-primary path, check whether the same relative
    path exists inside any active worktree. A match is a leakage fingerprint.
    """
    hits: list[tuple[Path, Path]] = []
    for rel in untracked:
        for wt in worktrees:
            candidate = wt / rel
            if candidate.exists():
                hits.append((rel, wt))
                break
    return hits


def main(argv: list[str]) -> int:
    clean = "--clean" in argv[1:]

    untracked = _untracked_in_primary()
    worktrees = _active_worktrees()

    if not untracked or not worktrees:
        print("ok [worktree-isolation] — no untracked files OR no active subagent worktrees.")
        return 0

    hits = _shadow_hits(untracked, worktrees)

    if not hits:
        print(
            f"ok [worktree-isolation] — {len(untracked)} untracked file(s) in primary, "
            f"none also present in {len(worktrees)} active subagent worktree(s)."
        )
        return 0

    print(f"FAIL [worktree-isolation] — {len(hits)} leak fingerprint(s) detected:")
    for rel, wt in hits:
        print(f"  {rel}  (also exists in {wt.relative_to(REPO_ROOT)})")
    print()

    if not clean:
        print(
            "These untracked files in the primary checkout shadow the same path inside a\n"
            "subagent's worktree. The subagent's commit is the canonical version; the\n"
            "primary's copy is the stale shadow from #208 leakage.\n\n"
            "Re-run with --clean to delete the primary's copies. The subagent's commit\n"
            "will land cleanly via cherry-pick afterwards."
        )
        return 1

    print("Deleting leaked copies from primary checkout:")
    for rel, _ in hits:
        target = REPO_ROOT / rel
        if target.exists():
            target.unlink()
            print(f"  removed: {rel}")
    print()
    print("Cleanup complete. Re-run without --clean to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
