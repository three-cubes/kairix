"""Mark SonarCloud security hotspots as Acknowledged with rationale.

Usage (locally with SONAR_TOKEN env var):
    SONAR_TOKEN=xxxx python3 scripts/sonar/triage_hotspots.py

Usage (CI):
    Triggered via .github/workflows/sonar-triage.yml workflow_dispatch.
    The workflow reads SONAR_TOKEN from GH secrets and runs this script.

Triage decisions are encoded in ``HOTSPOT_RATIONALES`` below — keyed by
``(rule_key, file_path)``. Each entry must include WHY the hotspot is a
false positive (or why we accept the documented risk). Hotspots whose
location does NOT match a rationale are left unchanged so they remain
visible for manual review.

This script is idempotent: running it multiple times is safe; already-
Acknowledged hotspots are skipped.

References:
- https://docs.sonarcloud.io/improving/security-hotspots/
- POST /api/hotspots/change_status — change status of a hotspot.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SONAR_BASE = "https://sonarcloud.io"
PROJECT_KEY = "quanyeomans_kairix"


# Triage decisions. Keyed by ``(rule_key, file_path)``; the value is the
# rationale Sonar will record. Missing entries are left unchanged.
#
# Pattern-match by file path — every hotspot in the listed file under the
# named rule gets the same rationale. Per-line nuance is rare; when needed,
# add ``(rule, file, line)`` keys (the loop below tries the more-specific
# key first).
HOTSPOT_RATIONALES: dict[tuple[str, str], str] = {
    # ── Regex DoS (S5852) ─────────────────────────────────────────────────────
    # All flagged regexes operate on bounded inputs (markdown frontmatter,
    # internal config files, document store paths, our own benchmark suites).
    # The "polynomial backtracking" warning fires on .* patterns even when
    # the input length is provably bounded; our corpus is private agent
    # output, not user-submitted content from an untrusted source.
    ("python:S5852", "kairix/core/temporal/chunker.py"): (
        "Bounded input — regex operates on internal document chunks split at known "
        "boundaries; chunk size is capped by KAIRIX_CHUNK_SIZE (default 4096). No "
        "untrusted input path. Reviewed and accepted."
    ),
    ("python:S5852", "kairix/knowledge/reflib/extract.py"): (
        "Bounded input — regex parses our own bundled reference library markdown. "
        "Files ship in the repo and are known to the kairix maintainers. No "
        "untrusted input path. Reviewed and accepted."
    ),
    ("python:S5852", "kairix/knowledge/reflib/frontmatter.py"): (
        "Bounded input — regex parses YAML frontmatter delimiters in markdown "
        "from the bundled reference library. Files ship in the repo. No "
        "untrusted input path. Reviewed and accepted."
    ),
    ("python:S5852", "kairix/knowledge/reflib/markdown.py"): (
        "Bounded input — regex parses our own markdown corpus structure. "
        "Files ship in the repo. No untrusted input path. Reviewed and accepted."
    ),
    ("python:S5852", "kairix/knowledge/reflib/splitter.py"): (
        "Bounded input — splitter operates on already-loaded markdown documents "
        "from the bundled reference library. No untrusted input path. Reviewed "
        "and accepted."
    ),
    ("python:S5852", "kairix/knowledge/store/crawler.py"): (
        "Bounded input — crawler reads operator-controlled document store paths. "
        "Documents are agent-authored memory in the operator's vault, not "
        "external input. Reviewed and accepted."
    ),
    ("python:S5852", "kairix/knowledge/wikilinks/resolver.py"): (
        "Bounded input — wikilinks resolver operates on agent-authored markdown "
        "from the document store. Path strings are normalised at the boundary. "
        "Reviewed and accepted."
    ),
    ("python:S5852", "kairix/text.py"): (
        "Bounded input — frontmatter/whitespace utility regexes operate on "
        "single-line and bounded multi-line input from agent-authored documents. "
        "Reviewed and accepted; existing NOSONAR comments document the same "
        "rationale at line level."
    ),
    ("python:S5852", "scripts/audit-date-formats.py"): (
        "Bounded input — operator-only script for one-off auditing of corpus "
        "date formats. Not on any production code path; not user-facing. "
        "Reviewed and accepted."
    ),
    # ── Pseudorandom number generators (S2245) ───────────────────────────────
    # Every flagged use of `random` is for sampling/scheduling, not security.
    ("python:S2245", "kairix/knowledge/wikilinks/audit.py"): (
        "Non-security PRNG — used to sample documents for the wikilinks audit "
        "report. Sampling distribution does not need cryptographic randomness. "
        "Reviewed and accepted."
    ),
    ("python:S2245", "kairix/quality/eval/generate.py"): (
        "Non-security PRNG — used for benchmark suite generation (random "
        "document sampling for Generate-Pseudo-Labels). Sampling distribution "
        "does not need cryptographic randomness. Reviewed and accepted."
    ),
    ("python:S2245", "kairix/quality/eval/judge.py"): (
        "Non-security PRNG — used for retrieval-quality scoring sampling, not security. Reviewed and accepted."
    ),
    ("python:S2245", "scripts/build-reflib-queries.py"): (
        "Non-security PRNG — operator-only script that samples reference-library "
        "documents to build benchmark queries. Not on any production code path. "
        "Reviewed and accepted."
    ),
    # ── Tempfile in publicly writable directory (S5443) ──────────────────────
    # All occurrences are in TEST files using deliberately-fixed paths under
    # /tmp/test-kairix.sqlite or similar — controlled test environments where
    # parallel-test isolation is via pytest's tmp_path fixture (separate test
    # cases) rather than a per-call temp file.
    ("python:S5443", "tests/embed/test_use_cases.py"): (
        "Test fixture — uses a fixed /tmp/test-kairix.sqlite path string in a "
        "stand-in DB-path callable. The callable is INJECTED via PipelineDeps "
        "and never opens the file (the test's fake DB ignores the path). "
        "No actual file is created. Reviewed and accepted."
    ),
    ("python:S5443", "tests/test_worker.py"): (
        "Test fixture — uses a fixed /tmp/test path string in EmbedPipelineResult "
        "construction. Test does not open any file at that path. No actual "
        "file is created. Reviewed and accepted."
    ),
    # ── Synchronization primitives (S4828) ───────────────────────────────────
    # Sonar flags any signal-based test as risky. The flagged tests are
    # intentionally testing the worker's SIGTERM/SIGINT handling.
    ("python:S4828", "tests/test_worker.py"): (
        "Test of signal-handling code — the worker registers SIGTERM/SIGINT "
        "handlers for graceful shutdown, and these tests verify the handlers "
        "run. Not production code. Reviewed and accepted."
    ),
    # ── Docker root user (S6471 — vault-agent) ───────────────────────────────
    # Kairix runtime: same rationale (Sonar will pick this up after the new
    # scan; the runtime stage's S6471 location was Dockerfile:27, which after
    # our cleanup is now line 28 with the same `FROM python:3.12-slim` —
    # Sonar should re-flag at the new line; we triage both.
    ("docker:S6471", "Dockerfile"): (
        "Kairix runtime stays as root because host bind-mount ownership varies "
        "per deployment (documents/, /run/secrets/, custom config paths). "
        "Operators that want non-root use `docker-compose user: <uid>:<gid>` or "
        "user-namespace remapping. Reviewed and accepted; documented in the "
        "Dockerfile comment block."
    ),
    ("docker:S6471", "docker/vault-agent/Dockerfile"): (
        "vault-agent is a sidecar that runs only at boot to fetch secrets from "
        "an MSI-authenticated Key Vault, then exits. It writes to a tmpfs at "
        "/run/secrets/ and never accepts user input — root is the expected user "
        "for the upstream azure-cli base image. Reviewed and accepted."
    ),
}


def _api(method: str, path: str, token: str, **params: str) -> dict:
    """Call SonarCloud API. Token sent as Bearer in Authorization header.

    Raises ``urllib.error.HTTPError`` on non-2xx; caller handles.
    """
    url = SONAR_BASE + path
    body: bytes | None = None
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "kairix-sonar-triage/1.0",
    }
    if method == "GET" and params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    elif params:
        body = urllib.parse.urlencode(params).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def _list_hotspots(token: str) -> list[dict]:
    """Return all TO_REVIEW hotspots for the project."""
    out: list[dict] = []
    page = 1
    while True:
        data = _api(
            "GET",
            "/api/hotspots/search",
            token,
            projectKey=PROJECT_KEY,
            status="TO_REVIEW",
            ps="500",
            p=str(page),
        )
        hotspots = data.get("hotspots", [])
        out.extend(hotspots)
        if len(hotspots) < 500:
            break
        page += 1
    return out


def _file_path(component: str) -> str:
    """SonarCloud component is ``project_key:path/to/file.py`` — extract path."""
    return component.split(":", 1)[-1] if ":" in component else component


def _resolve_rationale(rule: str, path: str, line: int) -> str | None:
    """Pick the most specific rationale that matches.

    Tries (rule, path, line) first, then (rule, path).
    """
    triple = HOTSPOT_RATIONALES.get((rule, path, line))  # type: ignore[arg-type] — tolerate optional triple keys
    if triple is not None:
        return triple
    return HOTSPOT_RATIONALES.get((rule, path))


def _acknowledge(token: str, hotspot_key: str, comment: str) -> bool:
    """Mark a hotspot as REVIEWED + ACKNOWLEDGED with rationale comment.

    Returns True on success, False on failure (printed reason).
    """
    try:
        _api(
            "POST",
            "/api/hotspots/change_status",
            token,
            hotspot=hotspot_key,
            status="REVIEWED",
            resolution="ACKNOWLEDGED",
            comment=comment,
        )
        return True
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code} — {exc.reason}: {exc.read().decode('utf-8', errors='replace')[:200]}")
        return False


def main() -> int:
    token = os.environ.get("SONAR_TOKEN")
    if not token:
        print("ERROR: SONAR_TOKEN env var not set", file=sys.stderr)
        return 2

    hotspots = _list_hotspots(token)
    print(f"Found {len(hotspots)} TO_REVIEW hotspots")

    triaged = 0
    skipped_unmapped: list[tuple[str, str, int]] = []
    failed: list[str] = []

    for h in hotspots:
        rule = h.get("ruleKey", "")
        path = _file_path(h.get("component", ""))
        line = int(h.get("line", 0) or 0)
        key = h.get("key", "")
        rationale = _resolve_rationale(rule, path, line)

        if rationale is None:
            skipped_unmapped.append((rule, path, line))
            continue

        if _acknowledge(token, key, rationale):
            triaged += 1
            print(f"ACK   {rule:30s} {path}:{line}")
        else:
            failed.append(key)

        # Brief pause to be polite to the API.
        time.sleep(0.2)

    print()
    print(f"Triaged: {triaged}")
    print(f"Failed:  {len(failed)}")
    print(f"Unmapped (need rationale entry): {len(skipped_unmapped)}")
    for rule, path, line in skipped_unmapped:
        print(f"  - {rule}  {path}:{line}")

    return 0 if not failed and not skipped_unmapped else 1


if __name__ == "__main__":
    sys.exit(main())
