#!/bin/bash
set -e

# Load secrets if available (Docker secrets or sidecar pattern)
if [[ -f /run/secrets/kairix.env ]]; then
    set -a && . /run/secrets/kairix.env && set +a
fi

# Load .env if mounted (Docker Compose env_file alternative)
if [[ -f /opt/kairix/.env ]]; then
    set -a && . /opt/kairix/.env && set +a
fi

MODE="${1:-serve}"

case "$MODE" in
    serve)
        # Pay the factory-init + first-search costs BEFORE the MCP server
        # binds, so the agent's first tool_search lands on a warm pipeline
        # rather than ~8s of cold-start latency that gets committed to
        # agent memory as 'kairix is flaky' (#278). Soft-fail on partial
        # warm — graph subsystem failure shouldn't block MCP startup.
        echo "Warming kairix caches before binding MCP..."
        kairix warm || echo "warm-up partial — proceeding (some subsystems may be cold)"
        echo "Starting kairix MCP server on port 8080..."
        exec kairix mcp serve --transport http --host 0.0.0.0 --port 8080
        ;;
    embed)
        echo "Running incremental embed..."
        exec kairix embed
        ;;
    setup)
        echo "Starting setup wizard..."
        exec kairix setup
        ;;
    worker)
        # Worker is long-running; warm so the first hourly embed cycle
        # runs against a hot pipeline.
        echo "Warming kairix caches before worker loop..."
        kairix warm || echo "warm-up partial — proceeding"
        echo "Starting background worker (embed hourly, entity seed nightly)..."
        exec python -m kairix.worker
        ;;
    eval)
        echo "Indexing reference library..."
        kairix embed
        echo "Running reference library benchmark (200 cases, RRF k=10)..."
        kairix benchmark run --suite /opt/kairix/suites/reflib-gold-v3.yaml --collection reference-library
        echo ""
        echo "Baseline: weighted=0.901 NDCG@10=0.990 Hit@5=99.0%"
        echo "Config: RRF k=10, boosts off, vec_limit=10"
        ;;
    *)
        # Pass through to kairix CLI
        exec kairix "$@"
        ;;
esac
