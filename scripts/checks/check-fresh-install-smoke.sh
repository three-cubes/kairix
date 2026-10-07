#!/usr/bin/env bash
# F81 — fresh-install smoke: prove a stranger's fresh install boots.
#
# Replays the README / docker-compose.yml quick start from a clean temp
# directory: copy the shipped compose file, seed .env from .env.example,
# copy the example config, drop a tiny sample document, `docker compose
# up -d`, then assert the install actually works:
#
#   stage 1  container-healthy   /healthz/ready returns 200 (bounded wait)
#   stage 2  mcp-handshake       POST initialize + tools/list to /mcp
#                                returns more than zero tools
#   stage 3  setup-wizard        out-of-the-box (setup_wizard_web defaults
#                                ON), GET /setup/ returns 200 (in-container
#                                loopback curl — the wizard's operator-token
#                                guard intentionally skips loopback peers)
#   stage 3b wizard-choreography POST /setup/folder/scan returns the HTMX
#                                scan partial (200, kx- wrapper markup), and
#                                POST /setup/key drives the form→redirect
#                                choreography (303 Location: /setup/folder).
#                                Proves the wizard's POST/partial/redirect
#                                interaction works, not just the GET screen.
#   stage 3c wizard-remote-access  (#500) host-side curl through the
#                                published port arrives as the docker bridge
#                                gateway IP (never loopback): 403 without a
#                                token → tokened URL 303 + Set-Cookie → 200
#                                with the cookie. The REAL Docker-bridge
#                                proof the macOS unit tests can only stand in
#                                for.
#   stage 4  bm25-search         `kairix embed` ingests the sample doc into
#                                the BM25 index, `kairix search` finds it
#
# SCOPE (first cut): CI has no provider secrets, so the embedding /
# vector legs are EXCLUDED. The .env keeps .env.example's placeholder
# provider values; `kairix embed` ingests documents + builds the FTS
# index BEFORE the embed leg fails, and `kairix search` degrades to
# BM25-only. A follow-up cut adds the vector leg behind a CI secret.
#
# Parameters (env):
#   KAIRIX_IMAGE_TAG   image tag for ghcr.io/three-cubes/kairix (default:
#                      main). CI builds the candidate image locally and
#                      tags it ghcr.io/three-cubes/kairix:fresh-smoke.
#   KAIRIX_HOST_PORT   host port for the kairix container (default: 8080).
#
# Usage:
#   KAIRIX_IMAGE_TAG=fresh-smoke bash scripts/checks/check-fresh-install-smoke.sh
#
# Cleanup (compose down -v + temp-dir removal) always runs via the EXIT
# trap, pass or fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

IMAGE_TAG="${KAIRIX_IMAGE_TAG:-main}"
HOST_PORT="${KAIRIX_HOST_PORT:-8080}"
BASE_URL="http://127.0.0.1:${HOST_PORT}"
PROBE_TERM="quokka-lighthouse-cadence"
SAMPLE_DOC="fresh-install-sample.md"
# #500 — fixed operator token for the browser-shaped wizard stage. A fresh-
# install test fixture, not a real credential.
WIZARD_OPERATOR_TOKEN="fresh-smoke-operator-token-0500"  # pragma: allowlist secret — fresh-install test fixture
HEALTHY_WAIT_SECONDS=420
COMPOSE_PROJECT="kairix-fresh-smoke"

WORKDIR=""
COMPOSE_STARTED=0

compose() {
    docker compose --project-name "$COMPOSE_PROJECT" "$@"
}

cleanup() {
    local rc=$?
    if [[ "$COMPOSE_STARTED" == "1" && -n "$WORKDIR" ]]; then
        (cd "$WORKDIR" && compose down -v --remove-orphans) || true  # teardown is best-effort; the EXIT trap must still exit with the stage rc
    fi
    if [[ -n "$WORKDIR" ]]; then
        rm -rf "$WORKDIR" || true  # temp-dir removal is best-effort; never mask the real rc
    fi
    exit "$rc"
}
trap cleanup EXIT

# Bounded execution — `timeout` ships on the Linux CI runners; fall back
# to unbounded on hosts without it (macOS without coreutils).
run_bounded() {
    local secs="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "$secs" "$@"
    else
        "$@"
    fi
}

# F21-shaped stage failure: named stage, fix:/next: markers, container
# diagnostics, non-zero exit (cleanup still runs via the EXIT trap).
fail_stage() {
    local stage="$1" fix_msg="$2" next_msg="$3"
    echo "FAIL [fresh-install-smoke:${stage}]"
    echo "fix: ${fix_msg}"
    echo "next: ${next_msg}"
    if [[ "$COMPOSE_STARTED" == "1" && -n "$WORKDIR" ]]; then
        (
            cd "$WORKDIR" || exit 0
            echo "----- docker compose ps -----"
            compose ps || true  # diagnostics only — the stage is already failing
            echo "----- kairix logs (last 100 lines) -----"
            compose logs --tail 100 kairix || true  # diagnostics only — the stage is already failing
            echo "----- neo4j logs (last 30 lines) -----"
            compose logs --tail 30 neo4j || true  # diagnostics only — the stage is already failing
        )
    fi
    exit 1
}

echo "=== F81 fresh-install smoke (image tag: ${IMAGE_TAG}, port: ${HOST_PORT}) ==="

# ── stage 0: preflight ───────────────────────────────────────────────────────
for tool in docker curl python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        fail_stage "preflight" \
            "install ${tool} — the smoke drives the shipped docker-compose stack with it." \
            "re-run: KAIRIX_IMAGE_TAG=${IMAGE_TAG} bash scripts/checks/check-fresh-install-smoke.sh"
    fi
done
if ! docker compose version >/dev/null 2>&1; then
    fail_stage "preflight" \
        "install the docker compose v2 plugin (docker compose version must succeed)." \
        "re-run: KAIRIX_IMAGE_TAG=${IMAGE_TAG} bash scripts/checks/check-fresh-install-smoke.sh"
fi

# ── stage 0b: assemble the stranger's fresh directory ────────────────────────
WORKDIR="$(mktemp -d)" || fail_stage "workdir" \
    "mktemp -d failed — the runner has no writable temp dir for the fresh-install directory." \
    "check TMPDIR / disk space on the runner, then re-run the smoke"
echo "workdir: ${WORKDIR}"
cp "${REPO_ROOT}/docker-compose.yml" "${WORKDIR}/docker-compose.yml"
cp "${REPO_ROOT}/.env.example" "${WORKDIR}/.env"
cp "${REPO_ROOT}/kairix.config.example.yaml" "${WORKDIR}/kairix.config.yaml"
{
    echo ""
    echo "# fresh-install smoke overrides"
    echo "KAIRIX_IMAGE_TAG=${IMAGE_TAG}"
    echo "KAIRIX_HOST_PORT=${HOST_PORT}"
    # setup_wizard_web defaults ON (cutover) — no env override here, so the
    # wizard stages below prove the OOTB default reaches /setup, not an
    # env-forced enable. This guards against an accidental future default-revert.
    # #500 — pin a deterministic operator token so the browser-shaped stage
    # below can open the tokened URL without scraping the first-boot log.
    # (Unset, the container mints + prints one; pinning keeps the smoke
    # deterministic.) Not a real credential — a fresh-install test fixture.
    echo "KAIRIX_INFRA_OPERATOR_TOKEN=${WIZARD_OPERATOR_TOKEN}"  # pragma: allowlist secret — fresh-install test fixture
} >> "${WORKDIR}/.env"
mkdir -p "${WORKDIR}/documents/04-Agent-Knowledge"
cat > "${WORKDIR}/documents/${SAMPLE_DOC}" <<EOF
# Fresh install sample

This sample document proves the BM25 search leg on a fresh install.
Unique probe term: ${PROBE_TERM}.
EOF

# ── stage 0c: up ─────────────────────────────────────────────────────────────
cd "$WORKDIR"
COMPOSE_STARTED=1
if ! compose up -d --quiet-pull; then
    fail_stage "compose-up" \
        "read the compose error above — the shipped docker-compose.yml failed to start from a clean directory with only .env.example defaults." \
        "reproduce locally: copy docker-compose.yml + .env.example into an empty dir and run docker compose up -d"
fi

# ── stage 1: container reaches healthy ───────────────────────────────────────
echo -n "stage 1 container-healthy: waiting for ${BASE_URL}/healthz/ready "
deadline=$((SECONDS + HEALTHY_WAIT_SECONDS))
ready=0
while [[ $SECONDS -lt $deadline ]]; do
    code=$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/healthz/ready" || true)  # a refused connection while booting is expected; the code check below decides
    if [[ "$code" == "200" ]]; then
        ready=1
        break
    fi
    echo -n "."
    sleep 5
done
echo ""
if [[ "$ready" != "1" ]]; then
    fail_stage "container-healthy" \
        "the kairix container never served /healthz/ready=200 within ${HEALTHY_WAIT_SECONDS}s — read the kairix logs below for the boot failure (s6 init, first-boot kairix init, or the api process)." \
        "reproduce: docker compose up -d, then curl -v ${BASE_URL}/healthz/ready"
fi
echo "stage 1 container-healthy: OK"

# ── stage 2: MCP handshake ───────────────────────────────────────────────────
mcp_post() {
    curl -fsS -L -X POST "${BASE_URL}/mcp" \
        -H 'Content-Type: application/json' \
        -H 'Accept: application/json, text/event-stream' \
        -d "$1"
}
# Tolerates both plain-JSON and SSE-framed (data: {...}) responses.
extract_tool_count() {
    python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
if "data:" in raw:
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[len("data:"):].strip()
            break
obj = json.loads(raw)
print(len(obj["result"]["tools"]))
'
}

INIT_BODY='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"fresh-install-smoke","version":"0.0.0"}}}'
INIT_RESP=$(mcp_post "$INIT_BODY" || true)  # a failed POST leaves the body empty; the serverInfo/tool-count check below fails the stage
if ! grep -q '"serverInfo"' <<< "$INIT_RESP"; then
    echo "initialize response: ${INIT_RESP}"
    fail_stage "mcp-handshake" \
        "POST /mcp initialize did not return a serverInfo result — the MCP transport is not serving on the published port." \
        "reproduce: curl -X POST ${BASE_URL}/mcp -H 'Content-Type: application/json' -d '${INIT_BODY}'"
fi

LIST_BODY='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
LIST_RESP=$(mcp_post "$LIST_BODY" || true)  # a failed POST leaves the body empty; the serverInfo/tool-count check below fails the stage
TOOL_COUNT=$(echo "$LIST_RESP" | extract_tool_count 2>/dev/null || echo "0")
if [[ "$TOOL_COUNT" -lt 1 ]]; then
    echo "tools/list response: ${LIST_RESP}"
    fail_stage "mcp-handshake" \
        "tools/list returned ${TOOL_COUNT} tools — an agent connecting to this fresh install would see no kairix capabilities." \
        "reproduce: curl -X POST ${BASE_URL}/mcp -H 'Content-Type: application/json' -d '${LIST_BODY}'"
fi
echo "stage 2 mcp-handshake: OK (${TOOL_COUNT} tools)"

# ── stage 3: setup wizard answers out-of-the-box (default-ON) ─────────────────
# In-container loopback curl: the wizard's operator-token guard skips
# loopback peers by design (host-side requests arrive from the docker
# bridge gateway and would need the kairix-infra-operator-token secret).
WIZARD_CODE=$(compose exec -T kairix curl -s -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:8080/setup/" || true)  # curl failure leaves an empty code; the status assertion below fails the stage
if [[ "$WIZARD_CODE" != "200" ]]; then
    fail_stage "setup-wizard" \
        "GET /setup/ returned ${WIZARD_CODE} (expected 200) on a default install (setup_wizard_web defaults ON) — the in-box wizard is not reachable out-of-the-box." \
        "reproduce: docker compose exec kairix curl -v http://127.0.0.1:8080/setup/"
fi
echo "stage 3 setup-wizard: OK (200)"

# ── stage 3b: wizard choreography (POST → HTMX partial → redirect) ────────────
# The GET-200 leg above proves the wizard SERVES; this leg proves it
# INTERACTS. All curls are in-container loopback (the operator-token
# guard skips loopback by design, as in stage 3).
#
# Leg 1 — POST /setup/folder/scan returns the HTMX scan partial. The
# sample doc lives at /data/documents (KAIRIX_DOCUMENT_ROOT), so the
# scan succeeds and the partial carries the `kx-scan-result` wrapper;
# either way a real scan partial is `kx-`-prefixed markup, never a 404
# or a raw 500.
SCAN_BODY=$(compose exec -T kairix curl -s \
    -X POST "http://127.0.0.1:8080/setup/folder/scan" \
    --data-urlencode "folder_path=/data/documents" || true)  # curl failure leaves an empty body; the partial-markup assertion below fails the stage
if ! grep -q 'kx-scan-result\|kx-validation-error' <<< "$SCAN_BODY"; then
    echo "scan partial body (head):"
    echo "$SCAN_BODY" | head -20
    fail_stage "wizard-choreography" \
        "POST /setup/folder/scan did not return the HTMX scan partial (expected kx-scan-result / kx-validation-error markup) — the wizard's POST/partial interaction is broken on a fresh install." \
        "reproduce: docker compose exec kairix curl -X POST http://127.0.0.1:8080/setup/folder/scan --data-urlencode folder_path=/data/documents"
fi
echo "stage 3b wizard-choreography: scan partial OK"

# Leg 2 — POST /setup/key drives the form→redirect choreography: the
# key-save handler persists the provider pick and answers 303 to
# /setup/folder. `-w` prints the status + redirect Location so we assert
# the choreography, not just a 2xx. (A read-only config overlay would
# re-render 200 with a rescue banner instead of 303 — also a valid,
# non-5xx outcome — so the assertion accepts a 303 redirect to
# /setup/folder OR a 200 re-render, and fails only on 404/5xx.)
KEY_POST=$(compose exec -T kairix curl -s -o /dev/null \
    -w '%{http_code} %{redirect_url}' \
    -X POST "http://127.0.0.1:8080/setup/key" \
    --data-urlencode "provider=anthropic" \
    --data-urlencode "api_key=fresh-install-smoke-placeholder-key" || true)  # curl failure leaves an empty code; the case below fails the stage
KEY_CODE="${KEY_POST%% *}"
KEY_REDIRECT="${KEY_POST#* }"
case "$KEY_CODE" in
    303)
        case "$KEY_REDIRECT" in
            */setup/folder) echo "stage 3b wizard-choreography: key POST OK (303 → ${KEY_REDIRECT})" ;;
            *) fail_stage "wizard-choreography" \
                "POST /setup/key returned 303 but redirected to '${KEY_REDIRECT}' (expected .../setup/folder) — the form→redirect choreography points at the wrong screen." \
                "reproduce: docker compose exec kairix curl -i -X POST http://127.0.0.1:8080/setup/key --data-urlencode provider=anthropic --data-urlencode api_key=x" ;;
        esac
        ;;
    200)
        echo "stage 3b wizard-choreography: key POST OK (200 re-render — config likely read-only, rescue banner served, not a 500)"
        ;;
    *)
        fail_stage "wizard-choreography" \
            "POST /setup/key returned ${KEY_CODE} (expected 303 redirect to /setup/folder, or a 200 re-render on a read-only config) — the key-save choreography failed on a fresh install." \
            "reproduce: docker compose exec kairix curl -i -X POST http://127.0.0.1:8080/setup/key --data-urlencode provider=anthropic --data-urlencode api_key=x"
        ;;
esac
echo "stage 3b wizard-choreography: OK"

# ── stage 3c: browser-shaped remote access (#500) ────────────────────────────
# The REAL Docker/Linux proof the unit tests can only stand in for: a
# HOST-side curl through the published port arrives at the guard as the
# docker bridge gateway IP (e.g. 172.18.0.1) — NEVER loopback — exactly
# like a tunnelled browser. Stages 3/3b used in-container loopback curls
# precisely because no browser-shaped path existed; this stage exercises
# the tokened-URL → signed-cookie grant a browser actually uses.
#
# Leg 1 — a bridge-IP request with NO token must be refused (proves the
# guard is actually gating, not open to the bridge).
NOTOKEN_CODE=$(curl -s -o /dev/null -w '%{http_code}' "${BASE_URL}/setup/provider" || true)  # curl failure leaves an empty code; the status assertion below fails the stage
if [[ "$NOTOKEN_CODE" != "403" ]]; then
    fail_stage "wizard-remote-access" \
        "GET ${BASE_URL}/setup/provider from a non-loopback (bridge-IP) origin returned ${NOTOKEN_CODE} (expected 403) — the operator-token guard is not gating bridge traffic." \
        "reproduce: curl -i ${BASE_URL}/setup/provider"
fi

# Leg 2 — opening the tokened URL must 303 and hand back a Set-Cookie.
COOKIE_JAR="${WORKDIR}/wizard-cookies.txt"
GRANT_CODE=$(curl -s -o /dev/null -w '%{http_code}' -c "$COOKIE_JAR" \
    "${BASE_URL}/setup/?operator_token=${WIZARD_OPERATOR_TOKEN}" || true)  # curl failure leaves an empty code; the status assertion below fails the stage
if [[ "$GRANT_CODE" != "303" ]]; then
    fail_stage "wizard-remote-access" \
        "GET the tokened URL (/setup/?operator_token=...) from a bridge-IP origin returned ${GRANT_CODE} (expected 303 + Set-Cookie) — the cookie grant is broken on real Docker bridge networking." \
        "reproduce: curl -i -c cookies.txt '${BASE_URL}/setup/?operator_token=${WIZARD_OPERATOR_TOKEN}'"
fi
if ! grep -q 'kairix_operator_grant' "$COOKIE_JAR"; then
    echo "cookie jar contents:"
    cat "$COOKIE_JAR" || true  # diagnostics only — the stage is already failing
    fail_stage "wizard-remote-access" \
        "the tokened-URL grant set no kairix_operator_grant cookie — a browser would have nothing to carry to the next screen." \
        "reproduce: curl -i -c cookies.txt '${BASE_URL}/setup/?operator_token=${WIZARD_OPERATOR_TOKEN}'"
fi

# Leg 3 — the granted cookie must admit a later bridge-IP GET to a screen.
WITHCOOKIE_CODE=$(curl -s -o /dev/null -w '%{http_code}' -b "$COOKIE_JAR" \
    "${BASE_URL}/setup/provider" || true)  # curl failure leaves an empty code; the status assertion below fails the stage
if [[ "$WITHCOOKIE_CODE" != "200" ]]; then
    fail_stage "wizard-remote-access" \
        "GET ${BASE_URL}/setup/provider with the grant cookie from a bridge-IP origin returned ${WITHCOOKIE_CODE} (expected 200) — the signed cookie did not admit the browser to the wizard." \
        "reproduce: curl -i -b cookies.txt ${BASE_URL}/setup/provider"
fi
echo "stage 3c wizard-remote-access: OK (403 without token → 303 grant + cookie → 200 with cookie)"

# ── stage 4: BM25 search path ────────────────────────────────────────────────
# `kairix embed` scans the document root and (re)builds the FTS index
# BEFORE the embedding leg runs; with .env.example's placeholder provider
# credentials the embed leg fails, which is expected here (no provider
# secrets in CI) — the `|| true` scopes this cut to the BM25 leg.
echo "stage 4 bm25-search: ingesting (embed leg expected to fail without provider key)"
# `timeout` can only exec real binaries, not the compose() shell function
# (first run died with "timeout: failed to run command 'compose'"), so the
# bounded stages spell out the docker compose invocation.
EMBED_OUT=$(run_bounded 600 docker compose --project-name "$COMPOSE_PROJECT" \
    exec -T kairix kairix embed 2>&1 || true)  # embed leg is EXPECTED to fail without provider secrets; stage 4 asserts on search

# --json keeps the assertion immune to rich's non-TTY line wrapping.
SEARCH_OUT=$(run_bounded 300 docker compose --project-name "$COMPOSE_PROJECT" \
    exec -T kairix kairix search "$PROBE_TERM" --no-entity-card --json 2>&1 || true)  # command failure leaves the output without the probe term; the assertion below fails the stage
if ! grep -q "$SAMPLE_DOC" <<< "$SEARCH_OUT"; then
    echo "embed output (tail):"
    echo "$EMBED_OUT" | tail -20
    echo "search output:"
    echo "$SEARCH_OUT" | tail -30
    fail_stage "bm25-search" \
        "kairix search '${PROBE_TERM}' did not return ${SAMPLE_DOC} — the fresh install cannot retrieve a document it just ingested over the BM25 leg." \
        "reproduce: docker compose exec kairix kairix embed; docker compose exec kairix kairix search '${PROBE_TERM}' --json"
fi
echo "stage 4 bm25-search: OK (${SAMPLE_DOC} found)"

# ── stage 5: agent memory-write path (PLA-296/298) ───────────────────────────
# The standard compose mounts the document root read-only and 04-Agent-Knowledge
# as the one writable submount. A memory write must PERSIST (preferred overlay
# when writable, data-dir fallback when read-only) and be BM25-searchable
# immediately — the write path a stock deploy relies on, proven end to end.
MEMORY_PROBE="wombat-beacon-cadence-memory"
MEMORY_OUT=$(run_bounded 120 docker compose --project-name "$COMPOSE_PROJECT" \
    exec -T kairix kairix remember builder "decision: adopt the ${MEMORY_PROBE} rollout" --kind decision --json 2>&1 || true)  # command failure leaves the output without the probe term; the assertion below fails the stage
if ! grep -q "$MEMORY_PROBE" <<< "$MEMORY_OUT"; then
    echo "remember output:"
    echo "$MEMORY_OUT" | tail -20
    fail_stage "memory-write" \
        "kairix remember did not persist a memory on the stock compose (read-only document root + writable 04-Agent-Knowledge submount) — an agent cannot save what it learned. PLA-296 should fall back to the writable data dir when the overlay is read-only." \
        "reproduce: docker compose exec kairix kairix remember builder 'decision: test' --kind decision --json"
fi
MEMORY_SEARCH=$(run_bounded 120 docker compose --project-name "$COMPOSE_PROJECT" \
    exec -T kairix kairix search "$MEMORY_PROBE" --no-entity-card --json 2>&1 || true)  # command failure leaves the output without the probe term; the assertion below fails the stage
if ! grep -q "$MEMORY_PROBE" <<< "$MEMORY_SEARCH"; then
    echo "memory search output:"
    echo "$MEMORY_SEARCH" | tail -20
    fail_stage "memory-write" \
        "kairix search could not find the memory just written by kairix remember — the memory-write path is not indexed/searchable on a stock deploy." \
        "reproduce: docker compose exec kairix kairix remember builder 'decision: test ${MEMORY_PROBE}' --kind decision; docker compose exec kairix kairix search '${MEMORY_PROBE}' --json"
fi
# The honest per-agent writability probe (PLA-298) must be wired into onboard
# check. The human render lists every check by name (pass or fail); `--json`
# lists only failures, so we grep the human output for the probe's presence.
ONBOARD_OUT=$(run_bounded 120 docker compose --project-name "$COMPOSE_PROJECT" \
    exec -T kairix kairix onboard check 2>&1 || true)  # command failure leaves the output without the probe term; the assertion below fails the stage
if ! grep -q "agent_memory_writable" <<< "$ONBOARD_OUT"; then
    echo "onboard check output:"
    echo "$ONBOARD_OUT" | tail -30
    fail_stage "memory-write" \
        "kairix onboard check does not surface the agent_memory_writable probe — the honest writability leg (PLA-298) is not wired into the check set." \
        "reproduce: docker compose exec kairix kairix onboard check"
fi
echo "stage 5 memory-write: OK (memory persisted + searchable + writability probe wired)"

echo "=== F81 fresh-install smoke: ALL STAGES PASSED ==="
