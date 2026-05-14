#!/usr/bin/env bash
# kairix-vault-agent — fetches Azure KV secrets to shared tmpfs, refreshes every 8h
set +x  # NEVER enable tracing — secret values will leak to stderr
set -euo pipefail

KV_NAME="${KAIRIX_KV_NAME:?KAIRIX_KV_NAME must be set}"
SECRETS_DIR="${KAIRIX_SECRETS_DIR:-/run/secrets}"
REFRESH="${REFRESH_INTERVAL_SECONDS:-28800}"

fetch_and_write() {
    local tmpfile kv_name_local
    kv_name_local="$KV_NAME"
    tmpfile=$(mktemp "${SECRETS_DIR}/.secrets.XXXXXX")
    chmod 600 "$tmpfile"
    _fetch() { local secret_name="$1"; az keyvault secret show --vault-name "$kv_name_local" --name "$secret_name" --query value -o tsv 2>/dev/null || echo ""; }
    {
        echo "KAIRIX_LLM_API_KEY=$(_fetch kairix-llm-api-key)"
        echo "KAIRIX_LLM_ENDPOINT=$(_fetch kairix-llm-endpoint)"
        echo "KAIRIX_LLM_MODEL=$(_fetch kairix-llm-model)"
        echo "KAIRIX_EMBED_API_KEY=$(_fetch kairix-embed-api-key)"
        echo "KAIRIX_EMBED_ENDPOINT=$(_fetch kairix-embed-endpoint)"
        echo "KAIRIX_EMBED_MODEL=$(_fetch kairix-embed-model)"
        echo "KAIRIX_NEO4J_PASSWORD=$(_fetch kairix-neo4j-password)"
    } >> "$tmpfile"
    mv -f "$tmpfile" "${SECRETS_DIR}/kairix.env"
    chmod 600 "${SECRETS_DIR}/kairix.env"
    echo "[vault-agent] Secrets written at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

mkdir -p "$SECRETS_DIR"
fetch_and_write
while true; do
    sleep "$REFRESH"
    echo "[vault-agent] Refreshing secrets..."
    fetch_and_write
done
