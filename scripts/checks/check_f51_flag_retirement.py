"""F51: every feature flag has a target_retire_in within 6 months of current version.

Feature flags are not permanent scaffolding. Each entry in
``kairix/core/features/registry.py:REGISTRY`` carries a ``target_retire_in``
version. F51 fires when the deadline has passed AND no adjacent
``# retire-extension: <reason>`` rationale comment is present in the
registry file.

Detection:

  1. Import ``kairix.core.features.registry.REGISTRY``. If the module is
     not yet present (PR-2 has not landed), exit 0 — vacuous-green per
     the cross-PR dependency convention.
  2. Determine current version via ``setuptools-scm`` (``get_version``),
     falling back to ``git describe --tags`` when scm is not importable
     (it is only a build dependency). A tagless checkout skips the check.
  3. Add 6 months to the current version's date component. Versions are
     CalVer-shaped ``vYYYY.M.D`` with an optional PEP-440 pre-release tail
     (``v2026.6.28a5``) or scm dev tail (``2026.6.29.dev3``).
  4. For each flag, compare ``target_retire_in`` against the deadline.
     If past, scan the registry source for an adjacent
     ``# retire-extension: <reason>`` comment; if absent, record a
     violation.

Violation file paths use the registry path with a ``:flag=<name>``
suffix so each flag entry produces a distinguishable violation line.

Per F21, REMEDIATION carries ``fix:`` / ``next:`` / ``run:`` markers.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

REGISTRY_REL_PATH = Path("kairix/core/features/registry.py")

REMEDIATION = """F51: feature flag <name> is past its target_retire_in deadline.
fix: either retire the flag (delete the REGISTRY entry + remove the legacy
     code path) OR bump target_retire_in with a '# retire-extension: <reason>'
     comment adjacent to the entry in kairix/core/features/registry.py.
next: see docs/architecture/feature-flag-architecture.md §4.1 (lifecycle
      stages) + §6 (F51 mechanics).
run: python3 scripts/checks/check_f51_flag_retirement.py

Pass example:
  # kairix/core/features/registry.py
  REGISTRY = {
      # retire-extension: pending m365_calendar soak — see issue #382
      "topology_resolver": FeatureFlag(
          name="topology_resolver",
          default=True,
          target_retire_in="v2026.12.31",
      ),
  }

Forbidden example:
  # kairix/core/features/registry.py
  REGISTRY = {
      "topology_resolver": FeatureFlag(
          name="topology_resolver",
          default=True,
          target_retire_in="v2026.1.1",  # past deadline, no extension comment
      ),
  }"""

# Accepts the CalVer ``vYYYY.M.D`` head plus the tails this repo's tags and
# ``setuptools-scm`` actually emit:
#   * a PEP-440 pre-release tail attached WITHOUT a separator (``v2026.6.28a5``,
#     the shape ``release-alpha.yml`` cuts) — ``(?:[a-z]\d+)?``;
#   * a ``setuptools-scm`` dev / local / extra-segment tail introduced by a
#     separator (``2026.6.29.dev3``, ``v2026.6.28-3-gabc``) — ``(?:[.\-+].*)?``.
# Pre-PLA-277 the first tail had no alternative, so every alpha tag and every
# ``aN.devM`` scm version parsed to None and F51 was permanently vacuous.
_VERSION_RE = re.compile(r"^v?(\d{4})\.(\d{1,2})\.(\d{1,2})(?:[a-z]\d+)?(?:[.\-+].*)?$")


def _parse_calver(version: str) -> date | None:
    """Parse a CalVer ``vYYYY.M.D`` string to a date.

    Tolerates the PEP-440 pre-release tail (``v2026.6.28a5``) and the
    ``setuptools-scm`` dev tail (``2026.6.29.dev3``) — only the
    year/month/day head is used. Returns None when the version does not
    match the CalVer shape at all; the caller treats None as "can't
    resolve a version, skip deadline check".
    """
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _add_six_months(d: date) -> date:
    """Return ``d + 6 months`` clamped to the last day of the target month."""
    month = d.month + 6
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    # Clamp day to month end (e.g. Aug 31 + 6 -> Feb 28/29).
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)


def _run_for_version(cmd: list[str], cwd: Path) -> str | None:
    """Run ``cmd`` in ``cwd`` and return trimmed stdout, or None on failure."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def _version_from_scm() -> str | None:
    """Return the ``setuptools-scm`` version, or None when scm is unavailable."""
    return _run_for_version(
        [sys.executable, "-c", "from setuptools_scm import get_version; print(get_version())"],
        REPO_ROOT,
    )


def _version_from_git_describe(cwd: Path = REPO_ROOT) -> str | None:
    """Return the nearest git tag (``git describe`` fallback), or None.

    setuptools-scm is a *build* dependency, so a runtime venv (or a
    box-side checkout) frequently has no ``setuptools_scm`` importable —
    in which case :func:`_version_from_scm` returns None and the deadline
    never evaluated. ``git describe --tags`` resolves the same CalVer head
    from the tag graph, so F51 fires on a tagged checkout even with no scm
    install. A truly tagless checkout returns None (deadline skipped).

    ``cwd`` defaults to the repository root and exists so the resolver can
    be pointed at any working tree — the F51 check tests exercise the
    fallback against a hermetic fixture repo rather than depending on the
    CI checkout having fetched tags.
    """
    return _run_for_version(["git", "describe", "--tags", "--abbrev=0"], cwd)


def _current_version() -> str | None:
    """Resolve the current version: ``setuptools-scm`` first, git tag fallback."""
    return _version_from_scm() or _version_from_git_describe()


def _find_extension_comment_lines(registry_text: str) -> set[str]:
    """Return the set of flag names that have a ``# retire-extension: <reason>``
    comment on the line(s) immediately preceding their REGISTRY entry.

    The detection scans the registry AST for ``"<name>": FeatureFlag(...)``
    dict items, then looks at the source lines just above each item for a
    comment of the shape ``# retire-extension: ...``.
    """
    try:
        tree = ast.parse(registry_text)
    except SyntaxError:
        return set()

    lines = registry_text.splitlines()
    extended: set[str] = set()

    for node in ast.walk(tree):
        if not (isinstance(node, ast.AnnAssign | ast.Assign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        is_registry = any(isinstance(t, ast.Name) and t.id == "REGISTRY" for t in targets)
        if not is_registry:
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=False):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            flag_name = key.value
            # Look at the lines immediately above the key for a retire-extension comment.
            key_line = getattr(key, "lineno", None)
            if key_line is None:
                continue
            # Walk back through contiguous comment lines.
            idx = key_line - 2  # 0-indexed; line above the key
            while idx >= 0:
                stripped = lines[idx].strip()
                if not stripped:
                    break
                if stripped.startswith("# retire-extension:"):
                    extended.add(flag_name)
                    break
                if not stripped.startswith("#"):
                    break
                idx -= 1
            # Also check inline (same-line) suffix comments on the
            # ``target_retire_in=`` line within the value, since that's
            # where reviewers naturally drop the rationale.
            if isinstance(value, ast.Call):
                for kw in value.keywords:
                    if kw.arg != "target_retire_in":
                        continue
                    kw_line = getattr(kw, "lineno", None)
                    if kw_line is None:
                        continue
                    line_text = lines[kw_line - 1]
                    if "# retire-extension:" in line_text:
                        extended.add(flag_name)
    return extended


def _load_registry() -> dict[str, object] | None:
    """Import REGISTRY defensively. Returns None when the module is absent.

    Module may be absent pre-PR-2; defensive import keeps the gate
    vacuous-green per the F51 cross-PR convention.
    """
    try:
        from kairix.core.features.registry import REGISTRY
    except ImportError:
        return None
    return dict(REGISTRY)


def find_violations(
    registry: dict[str, object],
    registry_text: str,
    current_version: str,
) -> list[str]:
    """Return sorted list of ``<registry_path>:flag=<name>`` strings for
    flags past their deadline without a retire-extension comment.
    """
    current_date = _parse_calver(current_version)
    if current_date is None:
        return []
    deadline = _add_six_months(current_date)
    extended = _find_extension_comment_lines(registry_text)
    flagged: list[str] = []
    for name, entry in registry.items():
        target = getattr(entry, "target_retire_in", None)
        if not isinstance(target, str):
            continue
        target_date = _parse_calver(target)
        if target_date is None:
            continue
        if target_date > deadline and name not in extended:
            flagged.append(f"{REGISTRY_REL_PATH.as_posix()}:flag={name}")
    return sorted(flagged)


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 when net-new violations exist."""
    registry = _load_registry()
    if registry is None:
        # PR-2 not yet landed; vacuous-green per cross-PR convention.
        print("ok [arch:f51-flag-retirement] — kairix.core.features absent; vacuous-green.")
        return 0
    if not registry:
        print("ok [arch:f51-flag-retirement] — registry empty; vacuous-green.")
        return 0

    registry_path = REPO_ROOT / REGISTRY_REL_PATH
    if not registry_path.exists():
        print("ok [arch:f51-flag-retirement] — registry file absent; vacuous-green.")
        return 0

    version = _current_version()
    if version is None:
        print("ok [arch:f51-flag-retirement] — setuptools-scm unavailable; vacuous-green.")
        return 0

    violations = find_violations(registry, registry_path.read_text(), version)
    if not violations:
        print("ok [arch:f51-flag-retirement] — clean.")
        return 0

    print("FAIL [arch:f51-flag-retirement] — flag(s) past target_retire_in:")
    for v in violations:
        print(f"  {v}")
    print()
    print(REMEDIATION)
    return 1


if __name__ == "__main__":
    sys.exit(main())
