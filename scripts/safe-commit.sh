#!/usr/bin/env bash
# safe-commit.sh — commit only if ALL quality gates pass.
#
# Usage:
#   bash scripts/safe-commit.sh "commit message"
#
# Gates (in order, fail-fast):
#   1. ruff lint (includes isort import ordering via I rules)
#   2. ruff format (black-compatible formatting)
#   3. mypy --strict type checking
#   4. pytest (unit + bdd + contract) with per-invocation coverage XML
#      generation (coverage.safe-commit.<pid>.xml, removed on exit)
#   5. architecture fitness functions (F1-F30, including F7 per-file coverage
#      floor — mirrors CI's Stage 2 invocation exactly so the historical
#      safe-commit ↔ CI parity gap on F7 is closed)
#
# Escape hatch: KAIRIX_SKIP_COVERAGE=1 reverts to the pre-2026-05-21 behaviour
# of skipping coverage generation + F7 enforcement. Useful for focused
# refactors between commits in a series; CI still enforces F7 on push.
#   6. detect-secrets
#   7. confidential data check

set -euo pipefail

# --fast mode (opt-in): skip the full test suite + coverage + arch fitness +
# Sonar checks, run only lint + format + mypy + tests touching the staged
# diff. For commits that genuinely can't affect the product test surface —
# workflow YAML, doc-only edits, sonar-project.properties tweaks, Dockerfile
# build-only changes. The full gate stays the merge bar; --fast is the
# iteration loop. See CLAUDE.md "Local-first feedback loops" for guidance.
#
# --check mode (opt-in): the sub-45s WARM inner loop, tighter than --fast.
# Four stages only — (1) scoped ruff lint+format on the STAGED files, (2)
# dmypy (warm daemon mypy) instead of cold `mypy`, (3) staged-path fitness
# (run_checks.py --staged — 4b's sound precise per-rule selection), (4) the
# impacted tests touching the staged paths. The first run is COLD (dmypy
# daemon spins up, ~30s); every run after is WARM (~sub-second mypy). This
# makes the CLAUDE.md "<60s local feedback" promise true. The FULL gate
# (default safe-commit.sh) REMAINS the merge bar — --check does NOT replace
# CI; it is purely the local inner loop. See CLAUDE.md "How to commit".
#
# --pre-pr mode (opt-in): the PRE-PUSH integration leg (CI Stage 3 parity).
# The default/--fast/--check gates run only `unit or bdd or contract` (CI
# Stage 2). CI Stage 3 runs `pytest tests/ -m integration` as a SEPARATE tier
# the inner loop never replicated — so a change could be green locally and red
# in CI (PLA-281: a DI-seam change broke an integration-only fake, run_search's
# broad except swallowed the TypeError into empty results, safe-commit was
# green, and CI Stage 3 went red an hour later). --pre-pr replicates CI Stage 3
# EXACTLY: same `-m integration --maxfail=3` marker, same extras set (mirrors
# ci.yml Stage 3's `.[dev,agents,markitdown,pdf_fallback,ocr,pptx,docx,xlsx]`,
# synced into a dedicated env so the warm --all-extras inner-loop venv is left
# intact). It is VERIFY-ONLY: it does not commit and needs nothing staged.
# safe-commit green is NECESSARY BUT NOT SUFFICIENT — run `--pre-pr` after the
# normal gate has committed, before you push / open a PR / report done. It is
# deliberately OUT of the default/--fast/--check inner loops so the <60s
# inner-loop promise holds. See CLAUDE.md "How to commit".
FAST_MODE=0
CHECK_MODE=0
PRE_PR_MODE=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --fast) FAST_MODE=1 ;;
        --check) CHECK_MODE=1 ;;
        --pre-pr) PRE_PR_MODE=1 ;;
        *) ARGS+=("$arg") ;;
    esac
done
set -- "${ARGS[@]:-}"

# --pre-pr is verify-only and needs no commit message; every other mode does.
if [[ "$PRE_PR_MODE" != "1" && $# -lt 1 ]]; then
    echo "Usage: bash scripts/safe-commit.sh [--fast | --check | --pre-pr] \"commit message\""
    exit 1
fi

MESSAGE="${1:-}"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

# ── #483 guard: command substitution under `set -e` ──────────────────────────
# `VAR=$(cmd)` kills the whole script AT THE ASSIGNMENT when cmd exits
# non-zero — no FAIL line, no captured output; the log just ends at the
# stage's "..." prefix. Every gate that captures output into a variable
# MUST run through run_gate so the exit code lands in GATE_RC for
# explicit handling instead of tripping `set -e`.
GATE_OUT=""
GATE_RC=0
run_gate() {
    GATE_RC=0
    GATE_OUT=$("$@" 2>&1) || GATE_RC=$?
}

# Named-stage death report. Prints the tail of the captured output, a
# FAIL line carrying the stage name + exit code, and fix:/next: action
# markers, then exits non-zero. Used when a gate exits non-zero WITHOUT
# producing the failure shape its stage handler knows how to summarise
# (crash, collection error, coverage floor, concurrent-run collision).
gate_died() {
    local stage="$1" rc="$2" rerun="$3"
    echo -e "${RED}FAIL${NC} (${stage} stage died, rc=${rc})"
    echo "----- last 50 lines of ${stage} output -----"
    echo "$GATE_OUT" | tail -50
    echo "----- end ${stage} output -----"
    echo "fix: read the ${stage} tail above — the stage exited before producing a verdict (crash, collection error, coverage floor, or a concurrent safe-commit run colliding on shared artifacts)."
    echo "next: re-run the stage standalone: ${rerun}"
    exit "$rc"
}

# ── --pre-pr: the pre-push integration leg (CI Stage 3 parity) ───────────────
# Verify-only. Replicates CI Stage 3 exactly — same `-m integration
# --maxfail=3` marker, same extras — so "green locally" == "green in CI" for
# the integration tier the inner loop skips (PLA-281). Runs BEFORE the coverage
# trap + staged guard so it needs nothing staged and never commits. Placed
# early on purpose: the normal gate has already committed; this is the final
# integration confirmation before push. Every stage emits a named OK/FAIL
# verdict (F83 stage-ledger contract).
if [[ "$PRE_PR_MODE" == "1" ]]; then
    echo "=== Pre-PR gate (--pre-pr — CI Stage 3 integration tier parity) ==="

    # A dedicated env keeps the warm --all-extras inner-loop .venv intact and
    # is synced to EXACTLY CI Stage 3's extras (mirror ci.yml Stage 3
    # `pip install -e ".[dev,agents,markitdown,pdf_fallback,ocr,pptx,docx,xlsx]"`),
    # NOT --all-extras — an extra present locally but absent in CI would let a
    # test pass here and fail there, which is the exact parity gap this closes.
    PRE_PR_VENV=".venv-pre-pr"
    # --group fitness pulls the tc-fitness engine (PLA-286: moved out of the dev
    # extra so the published wheel stays PyPI-clean). Stage 3 collects the
    # tests/checks + tests/architecture conftests, which import tc_fitness.
    PRE_PR_EXTRAS=(--extra dev --extra agents --extra markitdown --extra pdf_fallback --extra ocr --extra pptx --extra docx --extra xlsx --group fitness)

    echo -n "  sync CI-parity env... "
    run_gate env UV_PROJECT_ENVIRONMENT="$PRE_PR_VENV" uv sync "${PRE_PR_EXTRAS[@]}"
    if [[ "$GATE_RC" -ne 0 ]]; then
        echo -e "${RED}FAIL${NC}"
        echo "$GATE_OUT" | tail -20
        echo "fix: the dedicated CI-parity venv could not be synced — see the tail above."
        echo "next: env UV_PROJECT_ENVIRONMENT=$PRE_PR_VENV uv sync ${PRE_PR_EXTRAS[*]}"
        exit 1
    fi
    echo -e "${GREEN}OK${NC}"

    echo -n "  integration tests (Stage 3)... "
    run_gate env UV_PROJECT_ENVIRONMENT="$PRE_PR_VENV" \
        uv run pytest tests/ -m integration --maxfail=3
    PRE_PR_OUT="$GATE_OUT"
    if grep -qE "[0-9]+ failed|^FAILED |^ERROR " <<< "$PRE_PR_OUT"; then
        echo -e "${RED}FAIL${NC}"
        echo "$PRE_PR_OUT" | grep -E "FAILED|ERROR|passed|failed|error" | tail -15
        echo "fix: the failing integration tests are listed above — this is the CI Stage 3 tier the inner loop skips."
        echo "next: re-run standalone: env UV_PROJECT_ENVIRONMENT=$PRE_PR_VENV uv run pytest tests/ -m integration --maxfail=3"
        exit 1
    fi
    if [[ "$GATE_RC" -ne 0 ]]; then
        gate_died "integration (Stage 3)" "$GATE_RC" "env UV_PROJECT_ENVIRONMENT=$PRE_PR_VENV uv run pytest tests/ -m integration --maxfail=3"
    fi
    PRE_PR_PASSED=$(grep -m1 -oE '[0-9]+ passed' <<< "$PRE_PR_OUT" || echo "0 passed")
    [[ -z "$PRE_PR_PASSED" ]] && PRE_PR_PASSED="0 passed"
    echo -e "${GREEN}OK${NC} ($PRE_PR_PASSED)"

    echo ""
    echo -e "${GREEN}--pre-pr complete: the CI Stage 3 integration tier is green. Safe to push / open a PR.${NC}"
    exit 0
fi

# ── #483 concurrency hardening: per-invocation coverage artifacts ────────────
# Two concurrent safe-commit runs used to collide on coverage.xml and the
# .coverage data file, killing pytest mid-write with no visible error.
# Each invocation now writes its own pair and removes them on exit;
# run-all.sh receives the per-invocation XML path via KAIRIX_COVERAGE_XML.
COVERAGE_XML="coverage.safe-commit.$$.xml"
COVERAGE_DATA=".coverage.safe-commit.$$"
cleanup_coverage_artifacts() { rm -f "$COVERAGE_XML" "$COVERAGE_DATA"; }
trap cleanup_coverage_artifacts EXIT

# 0. Empty-stage guard. safe-commit.sh does not auto-stage; running it
# without `git add` produced silent no-op "commits" that masked real
# failures (#208 side-finding). Fail loud here instead.
if git diff --cached --quiet; then
    echo -e "${RED}FAIL${NC}: nothing staged for commit"
    echo "fix: stage files with 'git add <paths>' before running safe-commit.sh"
    echo "next: 'git status' to see what's modified but not yet staged"
    exit 1
fi

# ── --check: the sub-45s WARM inner loop ─────────────────────────────────────
# Tighter than --fast: scoped lint/format on STAGED files + dmypy (warm
# daemon mypy) + staged-path fitness (run_checks.py --staged, 4b's sound
# precise selection) + impacted tests. Self-contained — runs its four stages,
# commits on green, and exits BEFORE the full gate. The full gate (default
# safe-commit.sh) is untouched; --check never falls through to it. Every
# stage emits a named OK/FAIL verdict (F83 stage-ledger contract).
if [[ "$CHECK_MODE" == "1" ]]; then
    echo "=== Check gates (--check — scoped lint/format + dmypy + staged fitness + impacted tests, <45s warm) ==="

    # Staged source files, split by language surface. --diff-filter=AM drops
    # deletions (no file to lint). run_gate keeps the capture from tripping
    # set -e (#483 / F83 sub-rule (a)).
    run_gate git diff --cached --name-only --diff-filter=AM
    if [[ "$GATE_RC" -ne 0 ]]; then
        gate_died "staged-file enumeration" "$GATE_RC" "git diff --cached --name-only --diff-filter=AM"
    fi
    ALL_STAGED="$GATE_OUT"
    # Python files among the staged set drive scoped lint/format + the
    # impacted-test import grep. grep returns 1 on no-match under set -e, so
    # the trailing rationale keeps it from aborting (#483 / F83 sub-rule (b)).
    STAGED_PY=$(echo "$ALL_STAGED" | grep -E '\.py$' || true)  # no staged *.py is a valid state, not an error
    STAGED_KAIRIX_PY=$(echo "$STAGED_PY" | grep -E '^kairix/' || true)  # impacted-test discovery keys off kairix/ modules

    # ── check-stage 1: scoped ruff lint + format on the STAGED *.py only ──────
    echo -n "  ruff lint (staged)... "
    if [[ -z "$STAGED_PY" ]]; then
        echo -e "${GREEN}OK${NC} (no staged *.py)"
    else
        mapfile -t STAGED_PY_ARGS <<< "$STAGED_PY"
        run_gate uv run ruff check "${STAGED_PY_ARGS[@]}" --quiet
        if [[ "$GATE_RC" -ne 0 ]]; then
            echo -e "${RED}FAIL${NC}"
            echo "$GATE_OUT" | tail -20
            echo "fix: lint errors in the staged files above."
            echo "next: uv run ruff check ${STAGED_PY_ARGS[*]} --fix"
            exit 1
        fi
        echo -e "${GREEN}OK${NC}"

        echo -n "  ruff format (staged)... "
        run_gate uv run ruff format --check "${STAGED_PY_ARGS[@]}"
        if [[ "$GATE_RC" -ne 0 ]]; then
            echo -e "${RED}FAIL${NC}"
            echo "$GATE_OUT" | tail -20
            echo "fix: formatting drift in the staged files above."
            echo "next: uv run ruff format ${STAGED_PY_ARGS[*]}"
            exit 1
        fi
        echo -e "${GREEN}OK${NC}"
    fi

    # ── check-stage 2: dmypy (warm daemon mypy) ──────────────────────────────
    # `dmypy run` starts the daemon if it isn't running, then type-checks
    # incrementally against the warm in-memory state. The FIRST run is cold
    # (~30s daemon spin-up + full check); every run after is warm (sub-second).
    # We check kairix/ --strict, identical to the full gate's `mypy kairix/
    # --strict`, so the verdict matches. The daemon is LEFT WARM on exit (no
    # dmypy stop) — that is the whole point of the inner loop. .dmypy.json is
    # gitignored so the status file never pollutes the tree.
    echo -n "  dmypy strict (warm)... "
    run_gate uv run dmypy status
    if [[ "$GATE_RC" -ne 0 ]]; then
        echo -n "(cold start — first run spins the daemon up, ~30s) "
    fi
    run_gate uv run dmypy run -- kairix/ --strict
    DMYPY_OUT="$GATE_OUT"
    if grep -qE "error:|Daemon crashed" <<< "$DMYPY_OUT"; then
        echo -e "${RED}FAIL${NC}"
        echo "$DMYPY_OUT" | grep -E "error:|Daemon crashed" | head -10
        echo "fix: the type errors are listed above."
        echo "next: uv run dmypy run -- kairix/ --strict"
        exit 1
    fi
    if [[ "$GATE_RC" -ne 0 ]]; then
        # Non-zero without an `error:` line: dmypy itself failed to launch
        # (bad config, missing venv, daemon couldn't bind) — surface it as a
        # named death rather than a silent set -e abort.
        gate_died "dmypy" "$GATE_RC" "uv run dmypy run -- kairix/ --strict"
    fi
    echo -e "${GREEN}OK${NC}"

    # ── check-stage 3: staged-path fitness (4b's sound precise selection) ─────
    # run_checks.py --staged runs ONLY the rules whose scope intersects the
    # staged files (file-local rules narrowed to staged ∩ scope; relational
    # rules run their full scope when a staged path is in scope; always-run
    # rules unconditional). Sound: no false negative on staged changes. The
    # full --all gate is the backstop.
    echo -n "  staged fitness... "
    run_gate uv run python scripts/checks/run_checks.py --staged --skip-coverage
    FITNESS_OUT="$GATE_OUT"
    if [[ "$GATE_RC" -ne 0 ]]; then
        echo -e "${RED}FAIL${NC}"
        echo "$FITNESS_OUT" | tail -30
        echo "fix: the failing fitness rules are listed above (see docs/architecture/fitness-functions.md)."
        echo "next: uv run python scripts/checks/run_checks.py --staged --skip-coverage"
        exit 1
    fi
    echo -e "${GREEN}OK${NC}"

    # ── check-stage 4: impacted tests (tests touching the staged paths) ───────
    # Same import-graph discovery as --fast: map staged kairix/*.py to dotted
    # module paths, grep tests/ for files importing them, run those. No staged
    # kairix source (or no test imports it) → no product tests to run.
    echo -n "  impacted tests... "
    if [[ -z "$STAGED_KAIRIX_PY" ]]; then
        echo -e "${GREEN}OK${NC} (no staged kairix/*.py — no impacted tests)"
    else
        IMPORT_PATHS=$(echo "$STAGED_KAIRIX_PY" | sed 's|/|.|g; s|\.py$||' | sort -u)
        CHECK_TEST_FILES=()
        for imp in $IMPORT_PATHS; do
            while IFS= read -r tf; do
                [[ -n "$tf" ]] && CHECK_TEST_FILES+=("$tf")
            done < <(grep -rl "$imp" tests/ --include='*.py' 2>/dev/null | sort -u)
        done
        if [[ "${#CHECK_TEST_FILES[@]}" -eq 0 ]]; then
            echo -e "${GREEN}OK${NC} (no tests import the staged modules)"
        else
            UNIQ_CHECK_TESTS=$(printf '%s\n' "${CHECK_TEST_FILES[@]}" | sort -u | head -50)
            mapfile -t CHECK_TEST_ARGS <<< "$UNIQ_CHECK_TESTS"
            run_gate uv run python -m pytest "${CHECK_TEST_ARGS[@]}" -x --timeout=30 \
                -m "unit or bdd or contract" --no-cov -q
            CHECK_TEST_OUT="$GATE_OUT"
            # `-x` stops at the first failure; under `-q` pytest then prints a
            # `FAILED <nodeid>` line but not always a `N failed` summary, so
            # match either — otherwise a single-failure run would fall through
            # to the generic gate_died path with a less actionable message.
            if grep -qE "[0-9]+ failed|^FAILED " <<< "$CHECK_TEST_OUT"; then
                echo -e "${RED}FAIL${NC}"
                echo "$CHECK_TEST_OUT" | grep -E "FAILED|passed|failed|error" | tail -10
                echo "fix: the failing tests are listed above."
                echo "next: uv run python -m pytest ${CHECK_TEST_ARGS[*]} -m 'unit or bdd or contract'"
                exit 1
            fi
            if [[ "$GATE_RC" -ne 0 ]]; then
                gate_died "impacted tests" "$GATE_RC" "uv run python -m pytest ${CHECK_TEST_ARGS[*]} -m 'unit or bdd or contract'"
            fi
            CHECK_PASSED=$(grep -m1 -oE '[0-9]+ passed' <<< "$CHECK_TEST_OUT" || echo "0 passed")
            [[ -z "$CHECK_PASSED" ]] && CHECK_PASSED="0 passed"
            echo -e "${GREEN}OK${NC} ($CHECK_PASSED, impacted-only, no coverage)"
        fi
    fi

    echo ""
    echo -e "${GREEN}--check complete (sub-45s warm inner loop). The full gate remains the merge bar. Committing.${NC}"
    git commit -m "$MESSAGE"
    exit $?
fi

if [[ "$FAST_MODE" == "1" ]]; then
    echo "=== Fast gates (--fast — lint + format + mypy + staged-impact tests) ==="
else
    echo "=== Quality gates ==="
fi

# 1. Lint (includes isort via ruff I rules)
# Scope kairix/ + tests/ + scripts/ to match what pre-commit's ruff hook
# scans in CI — local-vs-CI divergence here has cost round-trips already.
echo -n "  ruff lint... "
ruff check kairix/ tests/ scripts/ --quiet 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff check kairix/ tests/ scripts/ --fix"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 2. Format (black-compatible via ruff format)
echo -n "  ruff format... "
ruff format --check kairix/ tests/ scripts/ >/dev/null 2>&1 || { echo -e "${RED}FAIL${NC}"; echo "Run: ruff format kairix/ tests/ scripts/"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 2b. gofmt on every Go service (when present). Auto-discovered: any
# services/<name>/go.mod triggers a gofmt check on that module. Mirrors
# what the remote 'Go quality' workflow does — keeping this local saves
# a CI round-trip when a Go change is in the staged diff.
if command -v gofmt >/dev/null 2>&1; then
    while IFS= read -r gomod; do
        svc_dir="$(dirname "$gomod")"
        echo -n "  gofmt -s ($svc_dir)... "
        run_gate gofmt -s -l "$svc_dir"
        unformatted="$GATE_OUT"
        if [[ -n "$unformatted" || "$GATE_RC" -ne 0 ]]; then
            echo -e "${RED}FAIL${NC}"
            echo "  ${unformatted//$'\n'/$'\n'  }"
            echo "Run: gofmt -s -w $svc_dir"
            exit 1
        fi
        echo -e "${GREEN}OK${NC}"
    done < <(find services -mindepth 2 -maxdepth 2 -name go.mod 2>/dev/null)
fi

# 3. Type checking (strict — matches CI)
echo -n "  mypy strict... "
# Use `uv run mypy` so optional-extra deps (watchdog, markitdown, boto3 etc.)
# resolve from the project venv rather than the system Python's site-packages.
# Without `uv run`, system mypy can't see types for kairix.connectors.obsidian
# (FileSystemEventHandler) or kairix.extractors.markitdown (MarkItDown) and
# fires false-positive `[misc]` / `[no-any-return]` errors. CI uses uv run mypy.
run_gate uv run mypy kairix/ --strict
MYPY_OUT="$GATE_OUT"
if grep -q "error" <<< "$MYPY_OUT"; then
    echo -e "${RED}FAIL${NC}"
    echo "$MYPY_OUT" | grep "error" | head -10
    echo "Run: uv run mypy kairix/ --strict"
    exit 1
fi
if [[ "$GATE_RC" -ne 0 ]]; then
    # Non-zero without an "error" line: mypy itself crashed (bad config,
    # missing venv, usage error) — previously a silent set -e death.
    gate_died "mypy" "$GATE_RC" "uv run mypy kairix/ --strict"
fi
echo -e "${GREEN}OK${NC}"

# 4. Tests (with coverage to enable F7 enforcement in the next step)
#
# Invocation mirrors CI's Stage 2 exactly (.github/workflows/ci.yml: "Unit +
# BDD + Contract tests with coverage") so the safe-commit ↔ CI parity gap
# that historically hid F7 failures from agents (KAIRIX_TRACE memory:
# feedback_ci_parity_checklist) is closed. The per-invocation coverage XML
# emitted here is consumed by run-all.sh's F7 check below (#483: a shared
# coverage.xml made concurrent safe-commit runs corrupt each other).
#
# To temporarily skip the per-file coverage floor during a focused refactor
# (e.g. between commits in a coverage-lift series), set KAIRIX_SKIP_COVERAGE=1.
# This is an escape hatch — do NOT push commits whose F7 only passes because
# coverage was skipped; CI will still enforce it.
echo -n "  tests + coverage... "
if [[ "$FAST_MODE" == "1" ]]; then
    # --fast: run only tests that import any file in the staged diff.
    # Discovery is import-graph-based: grep imports of the staged source
    # modules across tests/ and run those test files.
    STAGED_KAIRIX=$(git diff --cached --name-only --diff-filter=AM | grep -E "^kairix/.*\.py$" || true)
    if [[ -z "$STAGED_KAIRIX" ]]; then
        echo -e "${GREEN}OK${NC} (no staged kairix/*.py — skipping product tests)"
        TEST_OUT="--fast: no kairix source touched, no tests to run"
        TEST_RC=0
        COVERAGE_SKIPPED=1
    else
        # Map kairix/foo/bar.py → kairix.foo.bar for import-grep
        IMPORT_PATHS=$(echo "$STAGED_KAIRIX" | sed 's|/|.|g; s|\.py$||' | sort -u)
        TEST_FILES=()
        for imp in $IMPORT_PATHS; do
            while IFS= read -r tf; do
                [[ -n "$tf" ]] && TEST_FILES+=("$tf")
            done < <(grep -rl "$imp" tests/ --include='*.py' 2>/dev/null | sort -u)
        done
        # Dedup
        UNIQ_TESTS=$(printf '%s\n' "${TEST_FILES[@]}" | sort -u | head -50)
        if [[ -z "$UNIQ_TESTS" ]]; then
            echo -e "${GREEN}OK${NC} (no tests import the staged modules)"
            TEST_OUT="--fast: no tests import the staged modules"
            TEST_RC=0
            COVERAGE_SKIPPED=1
        else
            mapfile -t TEST_FILE_ARGS <<< "$UNIQ_TESTS"
            run_gate uv run python -m pytest "${TEST_FILE_ARGS[@]}" -x --timeout=30 \
                -m "unit or bdd or contract" --no-cov
            TEST_OUT="$GATE_OUT"
            TEST_RC="$GATE_RC"
            COVERAGE_SKIPPED=1
        fi
    fi
elif [[ "${KAIRIX_SKIP_COVERAGE:-0}" == "1" ]]; then
    run_gate uv run python -m pytest tests/ -x --timeout=30 -m "unit or bdd or contract"
    TEST_OUT="$GATE_OUT"
    TEST_RC="$GATE_RC"
    COVERAGE_SKIPPED=1
else
    # COVERAGE_FILE points the .coverage data file at the per-invocation
    # path so concurrent safe-commit runs never collide (#483).
    run_gate env COVERAGE_FILE="$COVERAGE_DATA" \
        uv run python -m pytest tests/ -x --timeout=30 \
        -m "unit or bdd or contract" \
        --cov=kairix "--cov-report=xml:${COVERAGE_XML}" \
        --cov-fail-under=80
    TEST_OUT="$GATE_OUT"
    TEST_RC="$GATE_RC"
    COVERAGE_SKIPPED=0
fi
if grep -qE "[0-9]+ failed" <<< "$TEST_OUT"; then
    echo -e "${RED}FAIL${NC}"
    echo "$TEST_OUT" | grep -E "FAILED|passed|failed" | tail -10
    echo "fix: the failing tests are listed above."
    echo "next: re-run standalone: uv run python -m pytest tests/ -m 'unit or bdd or contract'"
    exit 1
fi
if [[ "${TEST_RC:-0}" -ne 0 ]]; then
    # Non-zero without a "N failed" summary: pytest died before producing
    # a verdict (collection error, INTERNALERROR, coverage floor breach,
    # concurrent-run artifact collision) — previously a silent set -e
    # death with the log ending at "tests + coverage...".
    gate_died "tests + coverage" "$TEST_RC" "uv run python -m pytest tests/ -m 'unit or bdd or contract'"
fi
# --fast may legitimately collect 0 tests (no kairix/*.py touched, or no
# tests import the staged modules); skip the no-tests-collected check then.
if [[ "$FAST_MODE" != "1" ]] && ! grep -qE "[0-9]+ passed" <<< "$TEST_OUT"; then
    echo -e "${RED}FAIL${NC} (no tests collected)"
    exit 1
fi
PASSED=$(grep -m1 -oE '[0-9]+ passed' <<< "$TEST_OUT" || echo "0 passed")
[[ -z "$PASSED" ]] && PASSED="0 passed"
if [[ "$FAST_MODE" == "1" ]]; then
    echo -e "${GREEN}OK${NC} ($PASSED, --fast: impacted-only, no coverage)"
elif [[ "$COVERAGE_SKIPPED" == "1" ]]; then
    echo -e "${GREEN}OK${NC} ($PASSED, coverage skipped via KAIRIX_SKIP_COVERAGE=1)"
else
    TOTAL_COV=$(grep -m1 -oE 'Total coverage: [0-9.]+%' <<< "$TEST_OUT")
    echo -e "${GREEN}OK${NC} ($PASSED, $TOTAL_COV)"
fi

# --fast mode: skip arch fitness + secrets + confidential + sonar + mutation.
# The full gate runs all of these; --fast trades safety for iteration speed
# on commits that genuinely can't affect their domain (workflow YAML, docs,
# sonar-project.properties). CI is still the merge bar. The mutation stage
# below is deliberately NOT in the --fast path — mutation testing is a
# correctness gate, not an iteration-loop check.
if [[ "$FAST_MODE" == "1" ]]; then
    echo -e "${GREEN}--fast complete. Committing.${NC}"
    git commit -m "$MESSAGE"
    exit $?
fi

# 4b. Mutation parity (diff-scoped) — the mechanical sabotage control
# (#499 Phase 1). Generates single-token mutants on the staged diff's
# CHANGED function bodies (==↔!=, <↔<=, and↔or, True↔False, ...) and runs
# the impacted tests against each; a mutant whose tests still PASS is a
# SURVIVOR (the tests assert presence, not behaviour). Hard-capped
# (≤20 mutants, ≤60s/mutant, ≤150s total) + diff-scoped, so a normal
# commit stays bounded — a docs-only diff runs it in ~0.1s.
#
# Escape hatch: KAIRIX_SKIP_MUTATION=1 skips it (same disclosure discipline
# as KAIRIX_SKIP_COVERAGE — do NOT push a commit whose only green run set
# this; the nightly mutation-suite.yml still ratchets survivors on main).
echo -n "  mutation parity... "
if [[ "${KAIRIX_SKIP_MUTATION:-0}" == "1" ]]; then
    echo -e "${GREEN}OK${NC} (skipped via KAIRIX_SKIP_MUTATION=1 — disclose in commit body)"
else
    run_gate uv run python scripts/checks/mutation_parity.py
    MUT_OUT="$GATE_OUT"
    if grep -q "FAIL mutation_parity" <<< "$MUT_OUT"; then
        echo -e "${RED}FAIL${NC}"
        echo "$MUT_OUT" | tail -30
        echo "fix: a mutant survived — the impacted tests pass with the logic changed. Strengthen the assertion that should pin it (see the per-survivor fix: lines above)."
        echo "next: re-run standalone: uv run python scripts/checks/mutation_parity.py"
        exit 1
    fi
    if [[ "$GATE_RC" -ne 0 ]]; then
        gate_died "mutation parity" "$GATE_RC" "uv run python scripts/checks/mutation_parity.py"
    fi
    echo -e "${GREEN}OK${NC} ($(grep -m1 -oE '[0-9]+ survivor\(s\) of [0-9]+ mutant\(s\) run' <<< "$MUT_OUT" || echo 'no mutable diff'))"
fi

# 5. Architecture fitness functions (F1-F30)
# F7 (per-file coverage floor) runs against the per-invocation coverage XML
# produced in step 4 (passed via KAIRIX_COVERAGE_XML so concurrent
# safe-commit runs never collide on a shared coverage.xml — #483), closing
# the historical safe-commit ↔ CI parity gap. Falls back to skip-mode when
# KAIRIX_SKIP_COVERAGE=1 was set in step 4.
echo -n "  arch fitness... "
if [[ "${COVERAGE_SKIPPED:-0}" == "1" ]]; then
    ARCH_OUT=$(bash scripts/checks/run-all.sh --skip-coverage 2>&1) || {
        echo -e "${RED}FAIL${NC}"
        echo "$ARCH_OUT" | tail -30
        echo "See docs/architecture/fitness-functions.md for remediation."
        exit 1
    }
else
    ARCH_OUT=$(KAIRIX_COVERAGE_XML="$COVERAGE_XML" bash scripts/checks/run-all.sh 2>&1) || {
        echo -e "${RED}FAIL${NC}"
        echo "$ARCH_OUT" | tail -30
        echo "See docs/architecture/fitness-functions.md for remediation."
        exit 1
    }
fi
echo -e "${GREEN}OK${NC}"

# 6. Secret detection — pre-commit hook mirrors CI; do not invoke `detect-secrets scan`
# directly here (it overwrites the baseline and only scans the path you pass it).
echo -n "  secrets... "
SECRETS_OUT=$(pre-commit run detect-secrets --all-files 2>&1) || true
if grep -q "Failed" <<< "$SECRETS_OUT"; then
    echo -e "${RED}FAIL${NC}"
    echo "$SECRETS_OUT" | tail -20
    echo "If a test fixture is a false positive, mark with: # pragma: allowlist secret"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

# 7. Confidential check
echo -n "  confidential... "
bash scripts/pre-commit-confidential-check.sh 2>/dev/null || { echo -e "${RED}FAIL${NC}"; exit 1; }
echo -e "${GREEN}OK${NC}"

# 8. Sonar per-file ratchet — deterministic parity against the committed
# baseline (.architecture/baseline/sonar-per-file*.json) so Sonar findings are
# batched and fixed pre-push, not discovered per-cycle. The gate compares the
# project's CURRENT per-file open-issue counts to the committed snapshot and
# fails any file over baseline. It is deterministic (no live leak period), so
# there is no skip flag — the only non-failure path is "SonarCloud unreachable
# -> warn + exit 0", which the check handles internally. Default scope is the
# working set; pass --all for the full-repo view.
# See docs/architecture/local-first-feedback-loops.md.
echo -n "  sonar per-file ratchet... "
run_gate python3 scripts/checks/check_sonar_new_code.py
if [[ "$GATE_RC" -ne 0 ]]; then
    echo -e "${RED}FAIL${NC}"
    echo "$GATE_OUT"
    exit 1
fi
echo -e "${GREEN}OK${NC}"

echo ""
echo -e "${GREEN}All gates passed. Committing.${NC}"
git commit -m "$MESSAGE"
