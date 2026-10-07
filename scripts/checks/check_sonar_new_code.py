"""Sonar zero-open-issue gate — no grandfathered debt, deterministic locally.

CI's SonarCloud gate fires on smells / bugs / vulnerabilities / security
hotspots. This script makes that gate reproducible locally: it reads the
project's CURRENT open issues and TO_REVIEW hotspots from SonarCloud's public
API (the ``main`` analysis) and fails on ANY that is still present in the
working copy. There is no committed baseline and no skip flag: since PLA-472
kairix carries zero open Sonar findings, so every finding is in scope.

"Still present in the working copy"
-----------------------------------
SonarCloud analyses ``main``, so an issue you just fixed locally stays open
there until the change merges and main re-scans. Each issue carries the lines
it is anchored to (its text range plus any same-file flow locations). An issue
counts as RESOLVED LOCALLY once any of those lines, padded by a few lines of
context, no longer appears unchanged in the working copy (compared against the
file at ``origin/main``) — the flagged code was changed — or once the file is
gone. Without the analysed revision it falls back to Sonar's line hash (MD5 of
the flagged line with all whitespace removed). A hotspot is resolved only when
its code changes or its file is gone; hotspots must be reviewed in SonarCloud.
CI's SonarCloud analysis stays authoritative; this is the local signal.

Behaviour
---------
  - Default (no flag): focus on the WORKING SET — the files changed in this
    change (staged + unstaged + untracked vs the merge-base with ``main``),
    mirroring how ``scripts/safe-commit.sh`` defines "this change".
  - ``--all``: every file in the project, regardless of what changed. Use this
    to pull the full failing set once and batch-fix.
  - ``--json``: machine-readable findings list for agent batches.
  - Exits 1 if any in-scope finding is still present locally; 0 otherwise.

Network failure (e.g. offline pre-commit) prints a warning and exits 0 so
local development isn't blocked by SonarCloud availability. This is the ONLY
"skip" path and it only fires when SonarCloud is genuinely unreachable. CI's
own ``1 · Quality gate`` job remains authoritative.

Rule-key -> local-fix recipe map lives in
``docs/architecture/local-first-feedback-loops.md``. When a new Sonar rule
appears, add a recipe row in that doc in the same commit that fixes it.

Reads from environment (overridable):

  - ``SONAR_PROJECT_KEY`` — default ``three-cubes_kairix``
  - ``SONAR_BRANCH``      — default ``main``
  - ``SONAR_API_BASE``    — default ``https://sonarcloud.io/api``
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from os import environ
from pathlib import Path

REMEDIATION = """sonar gate: at least one open SonarCloud finding is still present.

fix: each `<file>:<line> [<rule>] <message>` row below is an open SonarCloud
issue (or hotspot) whose flagged code is still in the working copy. kairix
carries zero open Sonar findings — there is no baseline to absorb it. Apply
the positive-pattern recipe for the rule in
docs/architecture/local-first-feedback-loops.md §Rule map, then re-run.

Pattern: for each row,
  1. read the finding (rule + message) printed below,
  2. apply the §Rule-map recipe at the printed file:line,
  3. re-run this script until clean,
  4. commit once via safe-commit.sh.

next: `python3 scripts/checks/check_sonar_new_code.py --all` — repeat until clean.
run: bash scripts/safe-commit.sh "<message>"

Pass example:
  $ python3 scripts/checks/check_sonar_new_code.py --all
  ok [sonar] — 0 open finding(s) present (full-repo scope).

Forbidden example:
  # suppressing the finding instead of fixing the code
  pattern = re.compile(r"(a+)+$")  # NOSONAR"""

DEFAULT_PROJECT_KEY = "three-cubes_kairix"
DEFAULT_BRANCH = "main"
DEFAULT_API_BASE = "https://sonarcloud.io/api"
HTTP_TIMEOUT_S = 10
_PAGE_CEILING = 40  # hard pagination ceiling — anonymous rate-limit safety
_INTER_PAGE_DELAY_S = 0.2  # courtesy delay against anonymous rate limits

_REPO_ROOT = Path(__file__).resolve().parents[2]


class SonarUnreachableError(Exception):
    """Raised when the SonarCloud public API cannot be reached.

    Callers translate this into the offline-tolerant warn+exit-0 path — the
    ONLY non-failure escape, and only when Sonar is genuinely down.
    """


def _api_get(url: str) -> dict:
    """GET a SonarCloud public-API URL anonymously and parse JSON.

    Raises ``SonarUnreachableError`` on any network-level failure so the
    caller can distinguish "Sonar is down" (warn + exit 0) from "Sonar
    answered and a file regressed" (fail).
    """
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SonarUnreachableError(str(exc)) from exc
    if not isinstance(parsed, dict):
        raise SonarUnreachableError(f"unexpected non-object response from {url}")
    return parsed


def _component_to_path(component: str) -> str:
    """``three-cubes_kairix:kairix/x.py`` -> ``kairix/x.py`` (repo-relative)."""
    return component.split(":", 1)[-1]


@dataclass(frozen=True)
class Finding:
    """One open SonarCloud issue or hotspot."""

    path: str
    line: int | None
    rule: str
    message: str
    kind: str  # "issue" | "hotspot"
    line_hash: str | None = None
    #: 1-based lines (as analysed on the branch) the finding is anchored to:
    #: its primary text range plus any same-file flow (secondary) locations.
    lines: tuple[int, ...] = ()


def _location_lines(issue: dict, path: str) -> tuple[int, ...]:
    """Primary text-range lines plus same-file flow-location lines of an issue."""
    ranges = [issue.get("textRange") or {}]
    for flow in issue.get("flows") or []:
        for loc in flow.get("locations") or []:
            if _component_to_path(loc.get("component", "")) == path:
                ranges.append(loc.get("textRange") or {})
    lines: set[int] = set()
    for rng in ranges:
        start, end = rng.get("startLine"), rng.get("endLine")
        if isinstance(start, int):
            lines.update(range(start, (end if isinstance(end, int) else start) + 1))
    return tuple(sorted(lines))


def _paginate(url_base: str, params: dict[str, str], items_key: str, total_of) -> list[dict]:
    """Page through an anonymous public-API search endpoint."""
    out: list[dict] = []
    page = 1
    page_size = 500
    while True:
        query = urllib.parse.urlencode({**params, "ps": str(page_size), "p": str(page)})
        data = _api_get(f"{url_base}?{query}")
        batch = data.get(items_key, [])
        out.extend(batch)
        if page * page_size >= total_of(data) or not batch:
            break
        page += 1
        if page > _PAGE_CEILING:
            break
        time.sleep(_INTER_PAGE_DELAY_S)
    return out


def fetch_findings(project_key: str, branch: str, api_base: str) -> list[Finding]:
    """Current open issues (smells/bugs/vulnerabilities) + TO_REVIEW hotspots."""
    issues = _paginate(
        f"{api_base}/issues/search",
        {"componentKeys": project_key, "branch": branch, "resolved": "false"},
        "issues",
        lambda d: d.get("total", 0),
    )
    hotspots = _paginate(
        f"{api_base}/hotspots/search",
        {"projectKey": project_key, "branch": branch, "status": "TO_REVIEW"},
        "hotspots",
        lambda d: d.get("paging", {}).get("total", 0),
    )
    findings = [
        Finding(
            path=(path := _component_to_path(i.get("component", "?"))),
            line=i.get("line"),
            rule=str(i.get("rule", "?")),
            message=str(i.get("message", "")),
            kind="issue",
            line_hash=i.get("hash"),
            lines=_location_lines(i, path),
        )
        for i in issues
    ]
    findings += [
        Finding(
            path=_component_to_path(h.get("component", "?")),
            line=h.get("line"),
            rule=str(h.get("ruleKey", h.get("securityCategory", "hotspot"))),
            message=str(h.get("message", "")),
            kind="hotspot",
            lines=(h["line"],) if isinstance(h.get("line"), int) else (),
        )
        for h in hotspots
    ]
    return findings


_WS = re.compile(r"\s")


def sonar_line_hash(line: str) -> str:
    """SonarCloud's line hash: MD5 hex of the line with all whitespace removed."""
    return hashlib.md5(_WS.sub("", line).encode("utf-8"), usedforsecurity=False).hexdigest()


#: Lines of context around a finding's locations that count as "its code" — a
#: fix often lands a line or two from where Sonar anchors the issue (the regex
#: body under ``re.compile(``, the text inside an ``<a>`` tag).
CONTEXT_LINES = 3


def present_locally(finding: Finding, repo_root: Path, base_text: str | None = None) -> bool:
    """True while the finding's code is still unchanged in the working copy.

    ``base_text`` is the file as SonarCloud analysed it (the branch revision).
    The finding counts as resolved locally once ANY line in its locations
    (primary range + same-file flows, padded by :data:`CONTEXT_LINES`) no
    longer appears in the working copy. Without ``base_text`` (or without
    location lines) fall back to the primary line hash: resolved once no
    working-copy line carries it. A hotspot with neither stays present while
    its file exists — review it in SonarCloud.
    """
    path = repo_root / finding.path
    if not path.is_file():
        return False
    try:
        current = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return True
    current_hashes = {sonar_line_hash(line) for line in current}
    if base_text is not None and finding.lines:
        base = base_text.splitlines()
        region = {
            n
            for line in finding.lines
            for n in range(line - CONTEXT_LINES, line + CONTEXT_LINES + 1)
            if 1 <= n <= len(base)
        }
        return all(sonar_line_hash(base[n - 1]) in current_hashes for n in region)
    if finding.line_hash is None:
        return True
    return finding.line_hash in current_hashes


def evaluate(
    findings: list[Finding],
    files_in_scope: set[str] | None,
    repo_root: Path,
    base_text_of: Callable[[str], str | None] = lambda _path: None,
) -> list[Finding]:
    """Pure verdict core: the in-scope findings still present locally.

    ``files_in_scope`` limits the gate to the working set; ``None`` is the
    full-repo (``--all``) view. ``base_text_of(path)`` returns the file as
    SonarCloud analysed it (``None`` when unknown). Split so tests can inject
    fake findings, a tmp repo root and base texts without touching the network.
    """
    remaining = [
        f
        for f in findings
        if (files_in_scope is None or f.path in files_in_scope) and present_locally(f, repo_root, base_text_of(f.path))
    ]
    return sorted(remaining, key=lambda f: (f.path, f.line or 0, f.rule))


def analysed_text(path: str, branch: str) -> str | None:
    """The file at the analysed branch tip (``origin/<branch>``, else ``<branch>``)."""
    for ref in (f"origin/{branch}", branch):
        try:
            out = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                cwd=_REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if out.returncode == 0:
            return out.stdout
    return None


def _git_lines(args: list[str]) -> list[str]:
    """Run a read-only git command, return non-empty stdout lines (or [])."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def working_set_files() -> set[str]:
    """Repo-relative paths changed in THIS change — mirror safe-commit's set.

    Union of:
      - diff vs the merge-base with ``origin/main`` (or ``main`` if the
        remote ref is absent),
      - unstaged + staged working-tree diffs,
      - untracked files.

    Falls back to the plain working-tree diff if no merge-base is resolvable
    (e.g. shallow checkout). Empty set means nothing changed.
    """
    base_ref = None
    for ref in ("origin/main", "main"):
        mb = _git_lines(["merge-base", "HEAD", ref])
        if mb:
            base_ref = mb[0]
            break
    files: set[str] = set()
    if base_ref:
        files.update(_git_lines(["diff", "--name-only", base_ref]))
    else:
        files.update(_git_lines(["diff", "--name-only", "HEAD"]))
    files.update(_git_lines(["diff", "--name-only"]))  # unstaged
    files.update(_git_lines(["diff", "--name-only", "--cached"]))  # staged
    files.update(_git_lines(["ls-files", "--others", "--exclude-standard"]))  # untracked
    return {f for f in files if f}


def _print_findings(findings: list[Finding]) -> None:
    print(f"sonar gate FAILED — {len(findings)} open finding(s) still present:")
    for f in findings:
        print(f"  {f.path}:{f.line if f.line is not None else '?'} [{f.rule}] {f.message}   ({f.kind})")
    print()
    print(REMEDIATION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--all",
        action="store_true",
        help="full-repo view (every open finding); default focuses on the working set",
    )
    parser.add_argument("--project-key", default=environ.get("SONAR_PROJECT_KEY", DEFAULT_PROJECT_KEY))
    parser.add_argument("--branch", default=environ.get("SONAR_BRANCH", DEFAULT_BRANCH))
    parser.add_argument("--api-base", default=environ.get("SONAR_API_BASE", DEFAULT_API_BASE))
    args = parser.parse_args(argv)

    try:
        findings = fetch_findings(args.project_key, args.branch, args.api_base)
    except SonarUnreachableError as exc:
        # The ONLY skip path — fires only when Sonar is genuinely down, never
        # as a routine bypass. CI's quality gate stays authoritative.
        print(f"warn: SonarCloud API unreachable ({exc}); skipping local Sonar check.", file=sys.stderr)
        print("next: CI's `1 · Quality gate` remains authoritative.", file=sys.stderr)
        return 0

    files_in_scope = None if args.all else working_set_files()
    remaining = evaluate(findings, files_in_scope, _REPO_ROOT, lambda p: analysed_text(p, args.branch))

    if args.json:
        json.dump(
            {
                "scope": "all" if args.all else "working-set",
                "findings": [
                    {"file": f.path, "line": f.line, "rule": f.rule, "message": f.message, "kind": f.kind}
                    for f in remaining
                ],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 1 if remaining else 0

    if not remaining:
        scope = "full-repo" if args.all else "working-set"
        print(f"ok [sonar] — 0 open finding(s) present ({scope} scope).")
        return 0

    _print_findings(remaining)
    return 1


if __name__ == "__main__":
    sys.exit(main())
