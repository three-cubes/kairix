"""F94: No runtime writes to system/OS paths.

kairix must run least-privilege — including on hardened / "verified-secure"
VMs with read-only root filesystems and policy that forbids privilege
escalation and writes to core OS locations. So production runtime code in
``kairix/**`` must NEVER write to a system/OS path. Config and collected
state are persisted to a writable, non-system, app-owned location resolved
through ``kairix.paths`` (the data dir ``/var/lib/kairix`` / ``KAIRIX_DATA_DIR``
/ XDG) and the config overlay (``KAIRIX_CONFIG_OVERLAY_PATH``); the
read-only base config under ``/etc`` is merged in at read time, never
written. (Canonical motivation: the wizard-save overlay path, #485/#492.)

What this flags (literal-target write calls in ``kairix/**``):

- ``open("/etc/...", "w"|"a"|"x"...)`` — a write-mode open on a system-path
  literal (a read-mode ``open("/etc/...")`` is fine — base config is read).
- ``Path("/etc/...").write_text(...)`` / ``.write_bytes(...)``.
- ``Path("/etc/...").open("w"...)``.

…where the leading path component is a core OS location: ``/etc``,
``/opt``, ``/usr``, ``/boot``, ``/sys``, ``/bin``, ``/sbin``, ``/lib``,
``/lib64``. ``/var`` is NOT flagged — ``/var/lib/kairix`` is the writable,
app-owned data dir; nor is ``/run`` (tmpfs, e.g. the secrets mount) or
``/tmp`` (scratch).

Scope + limits (kept deliberately tight to stay false-positive-free):

- Only ``kairix/**`` is scanned — dev tooling under ``scripts/`` and tests
  legitimately touch system paths, and the system *installer* is exempt
  (see below).
- Only LITERAL targets are caught (``Path("/etc/x")`` /
  ``open("/etc/x", ...)``). A write whose target is a *variable* must
  still resolve through ``kairix.paths``; the architecture already does
  this, so the realistic regression to guard is the quick hardcode, which
  is always a literal.

Allow-list:

- ``kairix/install/**`` — ``kairix init --system`` is an explicit,
  operator-invoked privileged install (it writes the systemd unit under
  ``/etc/systemd/system``). That is a deliberate install-time action, not
  runtime; hardened deployments use the container / pip path instead.

There is no grandfathering: every literal system-path write in scope
fails the gate at safe-commit + CI. Fix the write target at source.
Failure output follows F21: leads with the fix, names ``run:`` to re-run
the gate, and shows a Pass/Forbidden example.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Core OS / system locations that are read-only on hardened deployments.
# ``/var`` is intentionally excluded — ``/var/lib/kairix`` is the writable
# app data dir. ``/run`` (tmpfs / secrets mount) and ``/tmp`` are excluded.
_SYSTEM_PREFIXES = ("/etc", "/opt", "/usr", "/boot", "/sys", "/bin", "/sbin", "/lib", "/lib64")

# Only production runtime code is in scope; the system installer is exempt.
SCOPE_PREFIX = "kairix/"
EXEMPT_PREFIX = ("kairix/install/",)

REMEDIATION = """Resolve the write target through kairix.paths — to pass.

fix: persist runtime config/state to a writable, non-system location:
  - config: write through the overlay (write_config_updates(..., overlay_path=...))
    so the read-only base config under /etc is never mutated
  - data/state: resolve under kairix.paths.data_dir() (/var/lib/kairix /
    KAIRIX_DATA_DIR / XDG) — never a hardcoded /etc, /opt, /usr, ... path
next: re-run ``python3 scripts/checks/check_f94_no_system_path_writes.py`` to confirm green.
run: bash scripts/safe-commit.sh "fix(<area>): write to the data dir, not a system path"

Pass example:
  (paths.data_dir() / "state.json").write_text(payload)
  write_config_updates({"provider": p}, overlay_path=overlay, config_path=base)

Forbidden example:
  open("/etc/kairix/kairix.config.yaml", "w")
  Path("/opt/kairix/state.json").write_text(payload)
"""

_WRITE_MODE_CHARS = ("w", "a", "x")
_PATH_NAMES = frozenset({"Path", "PosixPath"})
_DIRECT_WRITE_ATTRS = frozenset({"write_text", "write_bytes"})


def _const_str(node: ast.expr | None) -> str | None:
    """Return the string value of a string-literal node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_system_path(value: str) -> bool:
    return any(value == pre or value.startswith(pre + "/") for pre in _SYSTEM_PREFIXES)


def _is_write_mode(node: ast.expr | None) -> bool:
    mode = _const_str(node)
    return mode is not None and any(c in mode for c in _WRITE_MODE_CHARS)


def _mode_arg(call: ast.Call, positional_index: int) -> ast.expr | None:
    """Resolve the ``mode`` argument — positional at ``positional_index`` or ``mode=``."""
    arg = call.args[positional_index] if len(call.args) > positional_index else None
    for kw in call.keywords:
        if kw.arg == "mode":
            arg = kw.value
    return arg


def _path_literal_target(value: ast.expr) -> str | None:
    """Return the system-path string when ``value`` is ``Path("/etc/...")``."""
    if (
        isinstance(value, ast.Call)
        and (
            (isinstance(value.func, ast.Name) and value.func.id in _PATH_NAMES)
            or (isinstance(value.func, ast.Attribute) and value.func.attr in _PATH_NAMES)
        )
        and value.args
    ):
        target = _const_str(value.args[0])
        if target is not None and _is_system_path(target):
            return target
    return None


def _open_write_target(call: ast.Call) -> str | None:
    """``open("/etc/x", "w")`` → the system path; read-mode opens return None."""
    if not (isinstance(call.func, ast.Name) and call.func.id == "open" and call.args):
        return None
    target = _const_str(call.args[0])
    if target is None or not _is_system_path(target):
        return None
    return target if _is_write_mode(_mode_arg(call, 1)) else None


def _path_write_target(call: ast.Call) -> str | None:
    """``Path("/etc/x").write_text(...)`` / ``.write_bytes(...)`` / ``.open("w")``."""
    if not isinstance(call.func, ast.Attribute):
        return None
    target = _path_literal_target(call.func.value)
    if target is None:
        return None
    if call.func.attr in _DIRECT_WRITE_ATTRS:
        return target
    if call.func.attr == "open" and _is_write_mode(_mode_arg(call, 0)):
        return target
    return None


def _scan_file(path: Path, rel: str) -> list[str]:
    """Return one ``<rel>:<lineno>`` string per system-path write call."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _open_write_target(node) or _path_write_target(node)
        if target is not None:
            hits.append(f"{rel}:{node.lineno}: write to system path {target!r}")
    return hits


def _is_exempt_path(rel: str) -> bool:
    if not (rel.startswith(SCOPE_PREFIX) and rel.endswith(".py")):
        return True
    return any(rel.startswith(p) for p in EXEMPT_PREFIX)


def collect_violations(root: Path, files: list[str]) -> list[str]:
    """Scan every in-scope tracked path in ``files`` (repo-relative, under ``root``).

    Returns every hit in every non-exempt file — there is no grandfathering.
    """
    return [
        hit
        for rel in files
        if not _is_exempt_path(rel) and (root / rel).is_file()
        for hit in _scan_file(root / rel, rel)
    ]


def main() -> int:
    try:
        files = subprocess.check_output(["git", "ls-files", "kairix"], cwd=ROOT, text=True).splitlines()
    except subprocess.CalledProcessError:
        print("FAIL F94 no_system_path_writes: could not enumerate tracked files", file=sys.stderr)
        return 1

    violations = collect_violations(ROOT, files)
    if violations:
        print("FAIL F94 no_system_path_writes: violations found", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print("", file=sys.stderr)
        print(REMEDIATION, file=sys.stderr)
        return 1

    print(f"ok F94 no_system_path_writes — clean ({len(files)} kairix files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
