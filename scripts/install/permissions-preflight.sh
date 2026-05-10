#!/usr/bin/env bash
# permissions-preflight.sh — idempotent host-side preflight for kairix.
#
# Runs as ExecStartPre= for kairix.service. Verifies and (where safe)
# fixes the host-side prerequisites that have historically caused
# kairix to crash-loop after reboot:
#
#   1. /opt/kairix/app/.env is readable by the kairix service user.
#      Symptom when broken: docker compose exits with
#      "open /opt/kairix/app/.env: permission denied" and systemd
#      restart-loops every 10 seconds (#167 evidence).
#
#   2. /run/secrets/kairix.env exists and is non-empty.
#      Symptom when broken: kairix.service starts, MCP server reports
#      ready=true on /healthz, but vector search returns 0 hits because
#      embedding credentials are missing (#167 evidence).
#
#   3. Required environment variables are populated when the secrets
#      file is read together with /opt/kairix/service.env.
#
# Exit codes:
#   0 — all preflight checks pass; kairix may start.
#   1 — preflight failed; kairix.service will not start. The failing
#       check is logged with an actionable message before exit.
#
# Idempotent: re-running the script is a no-op if everything is correct.

set -u

KAIRIX_USER="${KAIRIX_USER:-kairix}"
KAIRIX_GROUP="${KAIRIX_GROUP:-kairix}"
APP_DIR="${KAIRIX_APP_DIR:-/opt/kairix/app}"
ENV_FILE="${KAIRIX_ENV_FILE:-${APP_DIR}/.env}"
SECRETS_FILE="${KAIRIX_SECRETS_FILE:-/run/secrets/kairix.env}"

# Required env keys after secrets + service.env are merged. If any are
# missing, vector search will degrade to BM25-only.
REQUIRED_KEYS=(
    "KAIRIX_LLM_API_KEY"
    "KAIRIX_LLM_ENDPOINT"
    "KAIRIX_EMBED_API_KEY"
    "KAIRIX_EMBED_ENDPOINT"
)

log() {
    echo "[kairix-preflight] $*" >&2
}

fail() {
    log "FAIL: $*"
    exit 1
}

# ── 1. .env readable by service user ──────────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    fail ".env file missing: $ENV_FILE"
fi

# Self-heal ownership/perms when running as root and the file is owned
# by someone else (the #167 case where install left it openclaw:openclaw).
if [ "$(id -u)" = "0" ]; then
    current_owner=$(stat -c '%U:%G' "$ENV_FILE" 2>/dev/null || stat -f '%Su:%Sg' "$ENV_FILE")
    if [ "$current_owner" != "${KAIRIX_USER}:${KAIRIX_GROUP}" ]; then
        log "fixing ownership of $ENV_FILE: $current_owner → ${KAIRIX_USER}:${KAIRIX_GROUP}"
        chown "${KAIRIX_USER}:${KAIRIX_GROUP}" "$ENV_FILE" || fail "chown $ENV_FILE failed"
    fi
    current_mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null || stat -f '%Lp' "$ENV_FILE")
    if [ "$current_mode" != "640" ] && [ "$current_mode" != "0640" ]; then
        log "fixing mode of $ENV_FILE: $current_mode → 640"
        chmod 640 "$ENV_FILE" || fail "chmod $ENV_FILE failed"
    fi
fi

# Verify the service user can read .env (works when running as root via
# ExecStartPre= and as the service user via direct invocation).
if ! sudo -u "$KAIRIX_USER" test -r "$ENV_FILE" 2>/dev/null; then
    if ! [ -r "$ENV_FILE" ]; then
        fail ".env not readable: $ENV_FILE (user=$(id -un))"
    fi
fi

# ── 2. secrets file present and non-empty ────────────────────────────────
if [ ! -s "$SECRETS_FILE" ]; then
    fail "secrets file missing or empty: $SECRETS_FILE — is kairix-fetch-secrets.service enabled and running?"
fi

# ── 3. required env keys are present in the merged environment ──────────
# Source both files in a subshell so we don't leak vars into the parent.
missing_keys=""
for key in "${REQUIRED_KEYS[@]}"; do
    value=$(
        set +u
        # shellcheck disable=SC1090
        . "$SECRETS_FILE" 2>/dev/null
        # shellcheck disable=SC1090
        . "${APP_DIR}/.env" 2>/dev/null
        eval "echo \${${key}:-}"
    )
    if [ -z "$value" ]; then
        missing_keys="${missing_keys} ${key}"
    fi
done

if [ -n "$missing_keys" ]; then
    fail "required env keys missing after merging .env + secrets:${missing_keys}"
fi

log "ok — all host-side preflight checks pass"
exit 0
