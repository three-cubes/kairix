#!/usr/bin/env bash
# F34: kairix/core/connectors/** may not import kairix/connectors/** or
# kairix/extractors/**.
#
# Domain code talks to the per-source connector and per-format extractor
# layers through Protocols only (defined in kairix/core/protocols.py).
# This wrapper delegates to the AST detector; the Python script owns the
# gate verdict and prints the F21 action-marked remediation on failure.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="F34: kairix/core/connectors/<file>.py imports kairix/connectors/ or kairix/extractors/.
fix: route the call through a Protocol in kairix/core/protocols.py (SourceConnector / Extractor / BronzeStore / SilverProcessor) and inject the concrete plugin through the registry.
next: see docs/architecture/connector-ingestion-architecture.md §2-§3 for the canonical layer split and Protocol surface.
run: bash scripts/checks/check-f34-core-connector-layer-imports.sh"

# The detector embeds its own gate (gate() from the tc_fitness package; any violation fails)
# — mirrors the F26/F27 pattern. Echo the remediation if the script exits
# non-zero so operators get the action markers regardless of how the
# wrapper is invoked.
if ! python3 "${SCRIPT_DIR}/check_f34_core_connector_layer_imports.py"; then
    printf '\n%s\n' "$REMEDIATION"
    exit 1
fi
