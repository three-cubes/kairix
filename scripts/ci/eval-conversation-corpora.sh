#!/usr/bin/env bash
# eval-conversation-corpora.sh — Plan B-parity Week 4 Stream A.
#
# Discovers every reference-library/conversations/team-* corpus and
# runs `kairix eval <suite> --json` against it. Compares the result against
# the pinned baseline at reference-library/conversations/expected/<name>.json:
#
#   - Full SuiteResult baseline → enforce no >2pp regression (gate fails PR).
#   - Sentinel {"baseline": "not-yet-measured"} → record the run, do not gate
#     ("establishing baseline" mode).
#   - Missing baseline file → fail loud with the fix: marker.
#
# Per-corpus output lives at /tmp/conversation-eval/<name>-result.json. The
# workflow uploads that directory as an artifact for inspection.
#
# F21: actionable error messages carry fix:/next: markers.
# F10:  no `continue-on-error` silencers — every failure surfaces.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CORPUS_DIR="reference-library/conversations"
EXPECTED_DIR="$CORPUS_DIR/expected"
OUT_DIR="${OUT_DIR:-/tmp/conversation-eval}"
# EVAL_CMD overrides the eval invocation (word-split into argv). Default:
# the real `python3 -m kairix.cli eval`. Tests point it at a stub so the
# gate's exit-code handling is exercised without a live LLM provider.
read -r -a EVAL_ARGV <<< "${EVAL_CMD:-python3 -m kairix.cli eval}"
mkdir -p "$OUT_DIR"

# Detect sentinel-shaped baselines so we skip --regression-against on those.
# Sentinel shape: top-level object with a "baseline" key set to
# "not-yet-measured". Everything else is treated as a real SuiteResult and
# fed to the production regression gate inside `kairix eval`.
is_sentinel() {
    python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    sys.exit(2)
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except json.JSONDecodeError:
    sys.exit(3)
sys.exit(0 if data.get("baseline") == "not-yet-measured" else 1)
PY
}

shopt -s nullglob
suites=("$CORPUS_DIR"/team-*)
shopt -u nullglob

if [ "${#suites[@]}" -eq 0 ]; then
    echo "::error::no team-* corpora found under $CORPUS_DIR"
    echo "fix: seed at least one suite under $CORPUS_DIR/engagement-<name>/"
    echo "next: re-run after seeding (see $CORPUS_DIR/README.md for the layout)"
    exit 1
fi

overall_status=0
# Set when any corpus result is partial (LLM-judge failures): the gate then
# exits 3 (inconclusive) unless a hard failure (1) already applies.
inconclusive=0

for suite in "${suites[@]}"; do
    suite_name="$(basename "$suite")"
    expected_file="$EXPECTED_DIR/$suite_name.json"
    out_file="$OUT_DIR/$suite_name-result.json"

    echo "::group::Eval $suite_name"

    if [ ! -f "$expected_file" ]; then
        echo "::error::missing baseline file $expected_file"
        echo "fix: create $expected_file (sentinel shape ok: {\"baseline\": \"not-yet-measured\"})"
        echo "next: re-run after committing the baseline file"
        overall_status=1
        echo "::endgroup::"
        continue
    fi

    sentinel_rc=0
    is_sentinel "$expected_file" || sentinel_rc=$?

    # Capture stderr so missing-credentials errors can be distinguished from
    # real code regressions. CI runs that don't bear the kairix-provider-llm-
    # api-key secret (PR builds from forks, dev branches without OIDC) cannot
    # measure recall regression — treat that as "skipped" rather than failed.
    eval_err_log="$OUT_DIR/$suite_name-eval-err.log"

    eval_rc=0
    if [ "$sentinel_rc" -eq 0 ]; then
        # Sentinel — record the run, do not regression-gate.
        echo "Baseline for $suite_name is a sentinel — establishing baseline mode (no regression gate)."
        "${EVAL_ARGV[@]}" "$suite" --json > "$out_file" 2> "$eval_err_log" || eval_rc=$?
        if [ "$eval_rc" -ne 0 ]; then
            cat "$eval_err_log" >&2
            if grep -q "SecretNotFoundError\|kairix-provider-llm-api-key" "$eval_err_log"; then
                echo "::warning::kairix eval skipped on $suite_name — LLM API key not available in this CI environment (expected on PR builds without KV access)"
            else
                echo "::error::kairix eval failed on $suite_name (sentinel mode)"
                echo "fix: re-run locally — \`kairix eval $suite --json\` — and resolve the traceback"
                echo "next: commit the fix, push, and the gate will re-run"
                overall_status=1
            fi
        fi
    elif [ "$sentinel_rc" -eq 1 ]; then
        # Real SuiteResult baseline — enforce regression gate.
        echo "Baseline for $suite_name is pinned — regression gate enforced (>2pp = fail)."
        "${EVAL_ARGV[@]}" "$suite" --json --regression-against "$EXPECTED_DIR" > "$out_file" 2> "$eval_err_log" || eval_rc=$?
        if [ "$eval_rc" -ne 0 ]; then
            cat "$eval_err_log" >&2
            if [ "$eval_rc" -eq 3 ]; then
                echo "::error::$suite_name regression gate INCONCLUSIVE — the run or the pinned baseline is partial (LLM-judge failures)"
                echo "fix: check the LLM provider credentials / availability; never pin a partial result as a baseline"
                echo "next: re-run the gate once every question is judged"
                inconclusive=1
            elif grep -q "SecretNotFoundError\|kairix-provider-llm-api-key" "$eval_err_log"; then
                echo "::warning::$suite_name regression gate skipped — LLM API key not available in this CI environment (expected on PR builds without KV access)"
            else
                echo "::error::$suite_name regressed against pinned baseline"
                echo "fix: investigate the recall/extractor delta surfaced above"
                echo "next: re-run after the fix, then update $expected_file if the score improved"
                overall_status=1
            fi
        fi
    else
        echo "::error::baseline file $expected_file is malformed (json decode or read failed)"
        echo "fix: regenerate via \`kairix eval $suite --json > $expected_file\`"
        echo "next: commit the regenerated baseline and re-run"
        overall_status=1
        echo "::endgroup::"
        continue
    fi

    # Shared completeness check after EVERY successful eval — sentinel mode
    # included: a partial result is never recorded as a candidate baseline.
    if [ "$eval_rc" -eq 0 ] && [ -s "$out_file" ]; then
        coverage_rc=0
        python3 "$SCRIPT_DIR/judge_coverage.py" "corpus $suite_name" "$out_file" || coverage_rc=$?
        if [ "$coverage_rc" -eq 3 ]; then
            echo "fix: never pin this result as a baseline; re-run once every question is judged"
            inconclusive=1
        elif [ "$coverage_rc" -ne 0 ]; then
            overall_status=1
        fi
    fi

    # Surface the per-corpus pass-rate + per-category breakdown in the log.
    python3 - "$out_file" "$suite_name" <<'PY'
import json
import sys
from pathlib import Path

from kairix.quality.completeness import judge_failures, partial_warning

path = Path(sys.argv[1])
name = sys.argv[2]
if not path.exists() or path.stat().st_size == 0:
    print(f"  [warn] no result file at {path} — eval likely failed")
    sys.exit(0)
data = json.loads(path.read_text(encoding="utf-8"))
failures = judge_failures(data)
if failures:
    print("  " + partial_warning(f"corpus {name} — never pin it as a baseline", failures))
n_passed = data.get("n_passed", 0)
n_total = data.get("n_questions", 0)
pct = round(100 * n_passed / n_total) if n_total else 0
print(f"  Pass rate ({name}): {n_passed}/{n_total} ({pct}%) mean={data.get('mean_score', 0):.3f}")
for cat, stats in sorted((data.get("per_category") or {}).items()):
    cat_n = int(stats.get("n", 0))
    cat_passed = int(stats.get("passed", 0))
    cat_pct = round(100 * cat_passed / cat_n) if cat_n else 0
    print(f"    {cat:<14} {cat_passed}/{cat_n} ({cat_pct}%) mean={stats.get('mean', 0):.3f}")
PY

    echo "::endgroup::"
done

if [ "$overall_status" -ne 0 ]; then
    echo "::error::conversation-eval-gate: one or more corpora failed"
    echo "fix: see the per-corpus ::error lines above for the actionable next step"
    echo "next: re-run after the fix; results are in $OUT_DIR for download"
    exit "$overall_status"
fi

if [ "$inconclusive" -ne 0 ]; then
    echo "::error::conversation-eval-gate: INCONCLUSIVE — one or more corpus results are partial (LLM-judge failures)"
    echo "fix: check the LLM provider credentials / availability"
    echo "next: re-run the gate; partial results are never compared or pinned"
    exit 3
fi

exit 0
