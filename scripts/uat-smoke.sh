#!/usr/bin/env bash
# UAT smoke test for a deployed kairix instance.
#
# Designed to be run BY AGENTS post-deploy (no interactive prompts,
# structured output, deterministic exit code). Verifies that every
# dogfood-critical surface is reachable on the deployed VM:
#
#   1. MCP HTTP transport `/healthz` returns ready=true
#   2. MCP `/mcp` endpoint accepts a tools/list request
#   3. CLI `kairix --help` resolves and lists every documented subcommand
#   4. CLI `kairix config validate` accepts the deployed kairix.config.yaml
#   5. CLI `kairix onboard check` returns no critical issues
#   6. CLI `kairix benchmark run --system mock` produces phase gates
#   7. CLI `kairix search "<smoke query>"` returns at least one result
#      from the indexed knowledge store
#   8. CLI `kairix reference-library status` reports the bundled corpus
#
# Each check prints a single line of the form:
#   [PASS] <check-name> — <one-line summary>
#   [FAIL] <check-name> — <one-line failure reason>
#
# At the end, a summary of passes/fails is printed and the script exits
# 0 (all green) or 1 (any failures).
#
# Usage:
#   bash scripts/uat-smoke.sh [--mcp-url URL] [--smoke-query "QUERY"]
#
# Defaults:
#   --mcp-url      http://127.0.0.1:8182
#   --smoke-query  "what is kairix"

set -u

MCP_URL="http://127.0.0.1:8182"
SMOKE_QUERY="what is kairix"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mcp-url) MCP_URL="$2"; shift 2 ;;
        --smoke-query) SMOKE_QUERY="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: $0 [--mcp-url URL] [--smoke-query QUERY]"
            exit 0
            ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

PASS=0
FAIL=0
FAILED_CHECKS=()

pass() {
    local name="$1"
    local detail="$2"
    PASS=$((PASS + 1))
    echo "[PASS] ${name} — ${detail}"
    return 0
}

fail() {
    local name="$1"
    local detail="$2"
    FAIL=$((FAIL + 1))
    FAILED_CHECKS+=("${name}")
    echo "[FAIL] ${name} — ${detail}"
    return 0
}

# ── 1. MCP /healthz ready ─────────────────────────────────────────────────
check_healthz() {
    local out
    out=$(curl -fsS --max-time 5 "${MCP_URL}/healthz" 2>&1) || {
        fail "mcp-healthz" "could not reach ${MCP_URL}/healthz: ${out}"
        return
    }
    if echo "$out" | grep -q '"ready"\s*:\s*true'; then
        pass "mcp-healthz" "ready=true ($out)"
    else
        fail "mcp-healthz" "endpoint returned but not ready: $out"
    fi
    return 0
}

# ── 2. MCP /mcp tools/list responds ────────────────────────────────────────
check_mcp_tools_list() {
    local req='{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    local out
    out=$(curl -fsS --max-time 10 \
        -H "Content-Type: application/json" \
        -H "Accept: application/json,text/event-stream" \
        -d "$req" "${MCP_URL}/mcp" 2>&1) || {
        fail "mcp-tools-list" "POST /mcp failed: ${out}"
        return
    }
    # Streamable HTTP returns either JSON or SSE — tolerate both.
    if echo "$out" | grep -q '"tools"'; then
        local count
        count=$(echo "$out" | grep -oE '"name":\s*"[^"]+"' | wc -l | tr -d ' ')
        pass "mcp-tools-list" "${count} tool(s) registered"
    else
        fail "mcp-tools-list" "no tools array in response: ${out:0:200}"
    fi
}

# ── 3. CLI --help dispatches ──────────────────────────────────────────────
check_cli_help() {
    local out
    out=$(kairix --help 2>&1) || {
        fail "cli-help" "kairix --help exited non-zero: ${out:0:200}"
        return
    }
    # Every subcommand should be listed in the help banner.
    local missing=""
    for cmd in embed search entity curator contradict store mcp onboard timeline summarise classify brief benchmark wikilinks reference-library eval setup config; do
        if ! echo "$out" | grep -q -E "(^|\s)${cmd}\s"; then
            missing="${missing} ${cmd}"
        fi
    done
    if [[ -z "$missing" ]]; then
        pass "cli-help" "all 18 subcommands listed"
    else
        fail "cli-help" "missing subcommand(s) in help banner:${missing}"
    fi
    return 0
}

# ── 4. Config validation ──────────────────────────────────────────────────
check_config_validate() {
    local out
    out=$(kairix config validate 2>&1) || {
        fail "config-validate" "validation failed: ${out:0:300}"
        return
    }
    pass "config-validate" "kairix.config.yaml accepted"
}

# ── 5. Onboard check ──────────────────────────────────────────────────────
check_onboard() {
    local out rc
    out=$(kairix onboard check 2>&1)
    rc=$?
    if [[ $rc -eq 0 ]]; then
        pass "onboard-check" "no critical deployment issues"
    else
        fail "onboard-check" "exit ${rc}: $(echo "$out" | tail -3 | tr '\n' ' ')"
    fi
    return 0
}

# ── 6. Benchmark mock run ─────────────────────────────────────────────────
check_benchmark_mock() {
    local out rc
    out=$(kairix benchmark run --system mock --suite contract 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        fail "benchmark-mock" "benchmark run failed: $(echo "$out" | tail -3 | tr '\n' ' ')"
        return
    fi
    if echo "$out" | grep -q "weighted_total"; then
        local score
        score=$(echo "$out" | grep -oE '"weighted_total":\s*[0-9.]+' | head -1)
        pass "benchmark-mock" "phase gates produced (${score})"
    else
        fail "benchmark-mock" "no weighted_total in output"
    fi
    return 0
}

# ── 7. Search smoke ───────────────────────────────────────────────────────
check_search() {
    local out rc
    out=$(kairix search "${SMOKE_QUERY}" --limit 3 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        fail "search-smoke" "search failed: $(echo "$out" | tail -3 | tr '\n' ' ')"
        return
    fi
    # Accept either JSON output with a non-empty results list OR a
    # human-readable banner with at least one result.
    local hits
    hits=$(echo "$out" | grep -cE '^\s*[0-9]+\.\s|"score"' || true)
    if [[ "${hits:-0}" -gt 0 ]]; then
        pass "search-smoke" "${hits} hit(s) for \"${SMOKE_QUERY}\""
    else
        fail "search-smoke" "no hits returned"
    fi
    return 0
}

# ── 8. Reference library status ───────────────────────────────────────────
check_reflib_status() {
    local out rc
    out=$(kairix reference-library status 2>&1)
    rc=$?
    if [[ $rc -ne 0 ]]; then
        fail "reflib-status" "exit ${rc}"
        return
    fi
    if echo "$out" | grep -qE "([0-9]+)\s+sources?|installed"; then
        pass "reflib-status" "$(echo "$out" | head -1)"
    else
        fail "reflib-status" "unexpected output: $(echo "$out" | head -1)"
    fi
    return 0
}

# ── Run all checks ────────────────────────────────────────────────────────
echo "=== kairix UAT smoke (target: ${MCP_URL}) ==="
check_healthz
check_mcp_tools_list
check_cli_help
check_config_validate
check_onboard
check_benchmark_mock
check_search
check_reflib_status

echo
echo "=== Summary ==="
echo "PASS: ${PASS}"
echo "FAIL: ${FAIL}"
if [[ $FAIL -gt 0 ]]; then
    echo "Failed checks: ${FAILED_CHECKS[*]}"
    exit 1
fi
echo "All UAT checks passed."
exit 0
