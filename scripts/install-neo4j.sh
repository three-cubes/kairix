#!/usr/bin/env bash
# install-neo4j.sh — Install Neo4j Community Edition for kairix
#
# Usage:
#   bash install-neo4j.sh              # Docker (default)
#   bash install-neo4j.sh --docker     # Docker Compose
#   bash install-neo4j.sh --apt        # apt package (Debian/Ubuntu)
#
# Neo4j Community Edition is licensed under GPL v3.
# kairix communicates with Neo4j via the Bolt protocol using the Apache 2.0
# Python driver (neo4j>=5.0,<6.0). No GPL3 code is bundled with kairix.
#
# After install, set in /opt/kairix/service.env:
#   KAIRIX_NEO4J_URI=bolt://localhost:7687
#   KAIRIX_NEO4J_USER=neo4j
#   KAIRIX_NEO4J_PASSWORD=<password-you-set-below>

set -euo pipefail

MODE="${1:---docker}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set — generate one with: openssl rand -hex 16}"
INSTALL_DIR="${NEO4J_INSTALL_DIR:-/opt/neo4j}"

# ---------------------------------------------------------------------------
# GPL3 notice — required before any install action
# ---------------------------------------------------------------------------

echo ""
echo "Neo4j Community Edition Licence Notice"
echo "======================================="
echo "Neo4j Community Edition is licensed under the GNU General Public"
echo "Licence v3 (GPL3). This script installs Neo4j as an independent"
echo "process; kairix communicates with it via the Bolt protocol using"
echo "the Apache 2.0 Python driver. No GPL3 code is bundled with kairix."
echo ""
echo "Full licence: https://neo4j.com/licensing/"
echo ""
read -r -p "Continue? [y/N] " confirm
if [[ "${confirm,,}" != "y" ]]; then
    echo "Aborted."
    exit 1
fi

# ---------------------------------------------------------------------------
# Docker install (default)
# ---------------------------------------------------------------------------

_install_docker() {
    echo "→ Installing Neo4j via Docker Compose..."

    if ! command -v docker &>/dev/null; then
        echo "ERROR: docker not found. Install Docker first: https://docs.docker.com/get-docker/" >&2
        exit 1
    fi

    mkdir -p "${INSTALL_DIR}"

    cat > "${INSTALL_DIR}/docker-compose.yml" << COMPOSE
services:
  neo4j:
    image: neo4j:5-community
    restart: unless-stopped
    ports:
      - "7687:7687"
      - "7474:7474"
    environment:
      NEO4J_AUTH: "neo4j/${NEO4J_PASSWORD}"
      NEO4J_PLUGINS: "[]"
    volumes:
      - neo4j-data:/data
      - neo4j-logs:/logs
    healthcheck:
      test: ["CMD", "neo4j", "status"]
      interval: 10s
      timeout: 5s
      retries: 10

volumes:
  neo4j-data:
  neo4j-logs:
COMPOSE

    echo "→ Starting Neo4j..."
    docker compose -f "${INSTALL_DIR}/docker-compose.yml" up -d

    echo "→ Waiting for Neo4j Bolt port (7687)..."
    local attempts=0
    while ! nc -z localhost 7687 2>/dev/null; do
        attempts=$((attempts + 1))
        if [[ $attempts -ge 30 ]]; then
            echo "ERROR: Neo4j did not start within 30s. Check: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs" >&2
            exit 1
        fi
        sleep 1
    done

    echo "✓ Neo4j is reachable at bolt://localhost:7687"
    echo ""
    echo "Add to /opt/kairix/service.env:"
    echo "  KAIRIX_NEO4J_URI=bolt://localhost:7687"
    echo "  KAIRIX_NEO4J_USER=neo4j"
    echo "  KAIRIX_NEO4J_PASSWORD=${NEO4J_PASSWORD}"
}

# ---------------------------------------------------------------------------
# apt install (Debian/Ubuntu)
# ---------------------------------------------------------------------------

_install_apt() {
    echo "→ Installing Neo4j via apt..."

    if ! command -v apt-get &>/dev/null; then
        echo "ERROR: apt-get not found. Use --docker on non-Debian systems." >&2
        exit 1
    fi

    # Add Neo4j apt repository
    wget -qO - https://debian.neo4j.com/neotechnology.gpg.key | sudo apt-key add -
    echo 'deb https://debian.neo4j.com stable 5' | sudo tee /etc/apt/sources.list.d/neo4j.list
    sudo apt-get update -q
    sudo apt-get install -y neo4j

    # Set password
    sudo neo4j-admin dbms set-initial-password "${NEO4J_PASSWORD}"

    # Enable and start service
    sudo systemctl enable neo4j
    sudo systemctl start neo4j

    echo "→ Waiting for Neo4j to start..."
    local attempts=0
    while ! nc -z localhost 7687 2>/dev/null; do
        attempts=$((attempts + 1))
        if [[ $attempts -ge 30 ]]; then
            echo "ERROR: Neo4j did not start within 30s. Check: sudo journalctl -u neo4j -n 50" >&2
            exit 1
        fi
        sleep 1
    done

    echo "✓ Neo4j is reachable at bolt://localhost:7687"
    echo ""
    echo "Add to /opt/kairix/service.env:"
    echo "  KAIRIX_NEO4J_URI=bolt://localhost:7687"
    echo "  KAIRIX_NEO4J_USER=neo4j"
    echo "  KAIRIX_NEO4J_PASSWORD=${NEO4J_PASSWORD}"
}

# ---------------------------------------------------------------------------
# Run selected mode
# ---------------------------------------------------------------------------

case "${MODE}" in
    --docker)
        _install_docker
        ;;
    --apt)
        _install_apt
        ;;
    *)
        echo "Unknown mode: ${MODE}"
        echo "Usage: $0 [--docker | --apt]"
        exit 1
        ;;
esac

# ---------------------------------------------------------------------------
# Run onboard check to validate
# ---------------------------------------------------------------------------

echo ""
echo "→ Running kairix onboard check..."
if command -v kairix &>/dev/null; then
    kairix onboard check
else
    echo "(kairix not on PATH — run 'kairix onboard check' manually after setting KAIRIX_NEO4J_URI)"
fi
