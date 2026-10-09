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
# current result fails the step (exit 3). Partial prior artifacts are skipped
# while walking back to the newest COMPLETE prior; with none, the current
# complete run establishes the baseline (exit 0).
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

coverage_rc=0
python3 "$SCRIPT_DIR/judge_coverage.py" "current LoCoMo nightly" "$CURRENT_JSON" || coverage_rc=$?
if [ "$coverage_rc" -ne 0 ]; then
    exit "$coverage_rc"
fi

if ! command -v gh >/dev/null 2>&1; then
    echo "::error::gh CLI not available — nightly comparison cannot fetch the prior artifact"
    echo "fix: run this script inside a GitHub Actions job (gh is preinstalled there)"
    echo "next: skip locally or stub PRIOR_JSON=... for development"
    exit 1
fi

WORKFLOW_FILE="eval-locomo-nightly.yml"
TMP_PRIOR_DIR="$(mktemp -d)"

# Recent successful runs that are NOT the current one, newest first.
PRIOR_RUN_IDS="$(
    gh run list \
        --workflow="$WORKFLOW_FILE" \
        --status=success \
        --limit=10 \
        --json databaseId,headSha \
        --jq ".[] | select(.databaseId != ${GITHUB_RUN_ID:-0}) | .databaseId"
)" || PRIOR_RUN_IDS=""

if [ -z "$PRIOR_RUN_IDS" ]; then
    echo "No prior successful nightly run found — recording baseline only (no comment posted)."
    exit 0
fi

# Walk back from the newest successful run to the newest COMPLETE one. A partial
# prior (LLM-judge failures) is never a comparator — and must not block every
# later nightly — so it is skipped, as is a prior whose artifact cannot be read.
PRIOR_RUN_ID=""
PRIOR_JSON=""
while read -r candidate_id; do
    [ -n "$candidate_id" ] || continue
    candidate_dir="$TMP_PRIOR_DIR/$candidate_id"
    mkdir -p "$candidate_dir"
    # Artifacts are named locomo-nightly-<run_id>; download and locate the JSON.
    if ! gh run download "$candidate_id" \
        --name "locomo-nightly-${candidate_id}" \
        --dir "$candidate_dir" 2>/dev/null; then
        echo "::warning::could not download nightly artifact for run $candidate_id; trying an older run"
        continue
    fi
    candidate_json="$(find "$candidate_dir" -name 'locomo-nightly-*.json' -type f | head -n 1)" || candidate_json=""
    if [ -z "$candidate_json" ]; then
        echo "::warning::no JSON file inside the artifact for run $candidate_id; trying an older run"
        continue
    fi
    candidate_rc=0
    python3 "$SCRIPT_DIR/judge_coverage.py" "prior LoCoMo nightly (run $candidate_id)" "$candidate_json" \
        > "$candidate_dir/coverage.log" || candidate_rc=$?
    if [ "$candidate_rc" -ne 0 ]; then
        echo "::notice::skipping prior nightly run $candidate_id — not a complete result (exit $candidate_rc); trying an older run"
        continue
    fi
    PRIOR_RUN_ID="$candidate_id"
    PRIOR_JSON="$candidate_json"
    break
done <<< "$PRIOR_RUN_IDS"

if [ -z "$PRIOR_JSON" ]; then
    echo "::notice::no complete prior nightly — establishing baseline: this complete run becomes the comparison point (no comment posted)."
    exit 0
fi
echo "Comparing against prior nightly run $PRIOR_RUN_ID."

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
