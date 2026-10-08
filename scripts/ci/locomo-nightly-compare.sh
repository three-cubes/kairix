#!/usr/bin/env bash
# locomo-nightly-compare.sh — Plan B-parity Week 4 Stream A.
#
# Compares today's LoCoMo nightly result against the previous successful
# nightly run's artifact. If the pass-rate drops by more than 2pp,
# leaves a comment on the latest commit on develop so the regression
# surfaces in the dev loop.
#
# Inputs (from env):
#   GH_TOKEN     — token for the `gh` CLI (workflow injects github.token)
#   JSON_PATH    — path to today's SuiteResult JSON (set by run.sh)
#
# Partial results (LLM-judge failures) are never compared: a partial
# current result fails the step; a partial prior artifact fails too, rather
# than silently becoming the comparison baseline.
#
# F21 markers (fix:/next:) on every actionable failure path.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CURRENT_JSON="${JSON_PATH:-}"
if [ -z "$CURRENT_JSON" ] || [ ! -f "$CURRENT_JSON" ]; then
    echo "::error::expected current SuiteResult at JSON_PATH (got '$CURRENT_JSON')"
    echo "fix: ensure scripts/ci/locomo-nightly-run.sh ran first and set JSON_PATH"
    echo "next: re-run the workflow"
    exit 1
fi

if ! python3 "$SCRIPT_DIR/judge_coverage.py" "current LoCoMo nightly" "$CURRENT_JSON"; then
    exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "::error::gh CLI not available — nightly comparison cannot fetch the prior artifact"
    echo "fix: run this script inside a GitHub Actions job (gh is preinstalled there)"
    echo "next: skip locally or stub PRIOR_JSON=... for development"
    exit 1
fi

WORKFLOW_FILE="eval-locomo-nightly.yml"
TMP_PRIOR_DIR="$(mktemp -d)"

# Most recent successful run that is NOT the current one.
PRIOR_RUN_ID="$(
    gh run list \
        --workflow="$WORKFLOW_FILE" \
        --status=success \
        --limit=10 \
        --json databaseId,headSha \
        --jq ".[] | select(.databaseId != ${GITHUB_RUN_ID:-0}) | .databaseId" \
        | head -n 1
)"

if [ -z "$PRIOR_RUN_ID" ]; then
    echo "No prior successful nightly run found — recording baseline only (no comment posted)."
    exit 0
fi

# Artifacts are named locomo-nightly-<run_id>; download and locate the JSON.
if ! gh run download "$PRIOR_RUN_ID" \
    --name "locomo-nightly-${PRIOR_RUN_ID}" \
    --dir "$TMP_PRIOR_DIR" 2>/dev/null; then
    echo "::warning::could not download prior nightly artifact (run $PRIOR_RUN_ID); skipping comparison"
    echo "next: this is non-fatal — the comparison will resume once an artifact downloads cleanly"
    exit 0
fi

PRIOR_JSON="$(find "$TMP_PRIOR_DIR" -name 'locomo-nightly-*.json' -type f | head -n 1)"
if [ -z "$PRIOR_JSON" ]; then
    echo "::warning::no JSON file inside prior artifact; skipping comparison"
    exit 0
fi

if ! python3 "$SCRIPT_DIR/judge_coverage.py" "prior LoCoMo nightly (run $PRIOR_RUN_ID)" "$PRIOR_JSON"; then
    exit 1
fi

# Compute pass-rate delta in percentage points.
DELTA_PP="$(
    python3 - "$CURRENT_JSON" "$PRIOR_JSON" <<'PY'
import json
import sys
from pathlib import Path

def pass_rate(path: Path) -> float:
    data = json.loads(path.read_text(encoding="utf-8"))
    n = int(data.get("n_questions", 0) or 0)
    if n <= 0:
        return 0.0
    return 100.0 * int(data.get("n_passed", 0) or 0) / n

current = pass_rate(Path(sys.argv[1]))
prior = pass_rate(Path(sys.argv[2]))
# Positive delta = regression (drop). Negative = improvement.
print(f"{prior - current:.2f}")
PY
)"

echo "LoCoMo pass-rate delta (prior - current): ${DELTA_PP}pp"

# Only comment if regression > 2pp; improvements stay silent here (the CSV
# trend artifact carries the upside signal).
THRESHOLD_PP=2.0
REGRESSION="$(python3 -c "import sys; print('1' if float(sys.argv[1]) > float(sys.argv[2]) else '0')" "$DELTA_PP" "$THRESHOLD_PP")"

if [ "$REGRESSION" != "1" ]; then
    echo "No regression beyond ${THRESHOLD_PP}pp — no comment posted."
    exit 0
fi

# Find the latest commit on develop and post a comment.
DEVELOP_SHA="$(git rev-parse origin/develop 2>/dev/null || git rev-parse HEAD)"

COMMENT_BODY="$(python3 - "$CURRENT_JSON" "$PRIOR_JSON" "$DELTA_PP" <<'PY'
import json
import sys
from pathlib import Path

current = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
prior = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
delta_pp = float(sys.argv[3])

def pct(d: dict) -> str:
    n = int(d.get("n_questions", 0) or 0)
    p = int(d.get("n_passed", 0) or 0)
    return f"{p}/{n} ({round(100 * p / n) if n else 0}%)"

body = [
    "## LoCoMo nightly — regression detected",
    "",
    f"Pass rate dropped by **{delta_pp:.2f}pp** (threshold: 2pp).",
    "",
    f"- Prior run : {pct(prior)} mean={float(prior.get('mean_score', 0.0)):.3f}",
    f"- This run  : {pct(current)} mean={float(current.get('mean_score', 0.0)):.3f}",
    "",
    "fix: investigate the per-category breakdown in the run logs / CSV artifact.",
    "next: re-run after the fix lands — this comment is auto-generated and won't repeat unless the regression persists.",
]
print("\n".join(body))
PY
)"

gh api \
    "repos/${GITHUB_REPOSITORY}/commits/${DEVELOP_SHA}/comments" \
    --method POST \
    --field body="$COMMENT_BODY" \
    >/dev/null

echo "Posted regression comment on develop@${DEVELOP_SHA}"
