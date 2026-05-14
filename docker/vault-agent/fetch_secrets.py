#!/usr/bin/env python3
"""
vault-agent: fetch secrets from Azure Key Vault and write to a tmpfs secrets file.

Runs as a Docker sidecar alongside the kairix service. Fetches all required
secrets at startup, writes them to /run/secrets/kairix.env, creates
/run/secrets/.ready to signal readiness, then refreshes on a timer.

Authentication via DefaultAzureCredential — supports (in order):
  1. Managed Identity  — recommended on Azure VMs (AZURE_CLIENT_ID optional)
  2. Service Principal — set AZURE_CLIENT_ID + AZURE_CLIENT_SECRET + AZURE_TENANT_ID
  3. Azure CLI         — for local dev (`az login`)

Required environment variables:
  KAIRIX_KV_NAME   Azure Key Vault name (e.g. kv-example)

Optional:
  SECRETS_DIR              Where to write the secrets file (default: /run/secrets)
  REFRESH_INTERVAL_SECONDS How often to re-fetch from KV (default: 3600)

Secrets fetched (KV secret name → env var written to file):
  kairix-llm-api-key      → KAIRIX_LLM_API_KEY
  kairix-llm-endpoint     → KAIRIX_LLM_ENDPOINT
  kairix-llm-model        → KAIRIX_LLM_MODEL
  kairix-embed-api-key    → KAIRIX_EMBED_API_KEY
  kairix-embed-endpoint   → KAIRIX_EMBED_ENDPOINT
  kairix-embed-model      → KAIRIX_EMBED_MODEL
  kairix-neo4j-password   → KAIRIX_NEO4J_PASSWORD
"""

import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s vault-agent %(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("vault-agent")

SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))
SECRETS_FILE = SECRETS_DIR / "kairix.env"
READY_FILE = SECRETS_DIR / ".ready"
KV_NAME = os.environ.get("KAIRIX_KV_NAME", "")
REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "3600"))

# Azure Key Vault secret name → env var name
SECRET_MAP: dict[str, str] = {
    "kairix-llm-api-key": "KAIRIX_LLM_API_KEY",
    "kairix-llm-endpoint": "KAIRIX_LLM_ENDPOINT",
    "kairix-llm-model": "KAIRIX_LLM_MODEL",
    "kairix-embed-api-key": "KAIRIX_EMBED_API_KEY",
    "kairix-embed-endpoint": "KAIRIX_EMBED_ENDPOINT",
    "kairix-embed-model": "KAIRIX_EMBED_MODEL",
    "kairix-neo4j-password": "KAIRIX_NEO4J_PASSWORD",  # pragma: allowlist secret
}


def fetch_from_keyvault() -> dict[str, str]:
    """
    Fetch all secrets from Azure Key Vault.

    Returns a dict of {env_var_name: secret_value} for successfully fetched
    secrets. Missing secrets are logged as warnings but do not abort the run.
    """
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    kv_uri = f"https://{KV_NAME}.vault.azure.net"
    logger.info("Connecting to Key Vault: %s", kv_uri)

    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=kv_uri, credential=credential)

    fetched: dict[str, str] = {}
    for secret_name, env_var in SECRET_MAP.items():
        resolved = _fetch_single_secret(client, secret_name)
        if resolved is not None:
            fetched[env_var] = resolved

    logger.info("Resolved %d of %d secrets from Key Vault", len(fetched), len(SECRET_MAP))
    return fetched


def _fetch_single_secret(client: object, secret_name: str) -> str | None:
    """Fetch one secret from Key Vault. Returns None on any failure. Never logs values."""
    try:
        secret = client.get_secret(secret_name)
        return secret.value if secret.value else None
    except Exception:
        return None


def write_secrets_file(secrets: dict[str, str]) -> None:
    """
    Write secrets as KEY=VALUE env file. File is chmod 600 (owner read-only).
    """
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# kairix secrets — written by vault-agent",
        f"# KV: {KV_NAME}",
        "",
    ]
    for env_var, value in sorted(secrets.items()):
        safe_value = value.replace("\n", "").replace("\r", "")
        lines.append(f"{env_var}={safe_value}")

    # By-design: vault-agent writes secrets to tmpfs-backed file (chmod 600,
    # ephemeral, not persisted to disk). Documented in SECURITY.md §3.
    content = "\n".join(lines) + "\n"
    SECRETS_FILE.write_text(content, encoding="utf-8")  # nosec: intentional secret file write
    SECRETS_FILE.chmod(0o600)
    logger.info("Wrote %d secret(s) to secrets file", len(secrets))


def signal_ready() -> None:
    """Write the readiness marker file checked by the kairix container healthcheck."""
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text("ready\n", encoding="utf-8")
    READY_FILE.chmod(0o644)
    logger.info("Ready signal written")


def main() -> None:
    if not KV_NAME:
        logger.error("KAIRIX_KV_NAME is not set. Cannot fetch secrets. Exiting.")
        sys.exit(1)

    first_run = True
    consecutive_failures = 0

    while True:
        try:
            secrets = fetch_from_keyvault()
            if secrets:
                write_secrets_file(secrets)
                consecutive_failures = 0
                if first_run:
                    signal_ready()
                    first_run = False
                    logger.info(
                        "Startup complete: %d secret(s) loaded. Refreshing every %ds.",
                        len(secrets),
                        REFRESH_INTERVAL,
                    )
            else:
                consecutive_failures += 1
                logger.error(
                    "No secrets fetched (attempt %d). Check KAIRIX_KV_NAME and Azure auth.",
                    consecutive_failures,
                )
        except Exception:
            consecutive_failures += 1
            logger.exception("Unexpected error fetching secrets (attempt %d)", consecutive_failures)

        time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    main()
