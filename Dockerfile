# ── Build stage: install all dependencies ────────────────────────────────────
FROM python:3.12-slim AS builder

# Version passed in by the publishing workflow (docker-publish.yml). The
# .dockerignore excludes .git from the build context, so setuptools-scm
# can't auto-derive the version and would fall back to "0.0.0" (see
# fallback_version in pyproject.toml). Passing it as a build-arg and
# exporting SETUPTOOLS_SCM_PRETEND_VERSION lets pip install . compute the
# real version without needing the git history in-context (#267).
ARG KAIRIX_VERSION=0.0.0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${KAIRIX_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Explicit, glob-free file list — kairix is pyproject.toml-only; the legacy
# setup.cfg/setup.py globs from earlier images were a Sonar S6470 hotspot
# and didn't actually match anything in the source tree. Listing the real
# files removes the glob and the hotspot in one step.
COPY pyproject.toml /opt/kairix/src/pyproject.toml
COPY README.md /opt/kairix/src/README.md
COPY kairix/ /opt/kairix/src/kairix/

# Install PyTorch CPU-only first (prevents pulling ~5GB CUDA libs on GPU-less servers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir "/opt/kairix/src[neo4j,agents,nlp,rerank]" \
    && python -m spacy download en_core_web_sm || true

# ── Runtime stage: slim image with only installed packages ───────────────────
# Runtime stays as root (default for python:3.12-slim) so host bind mounts
# whose ownership varies per deployment (documents/, /run/secrets/, custom
# config paths) work without per-host UID coordination. Operators that
# want non-root override via `docker-compose user: <uid>:<gid>` or
# user-namespace remapping. The S6471 Sonar hotspot for this is
# documented in #174 and triaged as Acknowledged.
FROM python:3.12-slim AS runtime

# Copy installed Python packages from builder (no build-essential, no source)
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create runtime directories and install runtime-only system deps (curl for healthchecks)
RUN mkdir -p /data/documents /data/kairix /data/kairix/workspaces /opt/kairix/bin /opt/kairix/cron /opt/kairix/plugins \
    && apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Expose the kairix-bundled openclaw plugins at a stable path (#246 W5).
#
# Plugins ship as package-data under
# ``<site-packages>/kairix/plugins/openclaw/`` (configured in
# pyproject.toml). The symlink below means admins can point openclaw's
# ``plugins.load.paths`` at ``/opt/kairix/plugins/openclaw`` and not
# worry about Python's site-packages location moving between Python
# minor versions. The path matches the canonical openclaw config snippet
# documented in ``docs/operations/MCP-DEPLOYMENT.md`` and in each
# plugin's README.
RUN ln -s /usr/local/lib/python3.12/site-packages/kairix/plugins/openclaw /opt/kairix/plugins/openclaw

# Copy entrypoint and default config
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
COPY kairix.example.config.yaml /opt/kairix/kairix.config.yaml

# Reference library + evaluation suites (stable test corpus, ships with the container)
COPY reference-library/ /opt/kairix/reference-library/
COPY suites/ /opt/kairix/suites/

ENV KAIRIX_DB_PATH=/data/kairix/index.sqlite \
    KAIRIX_DOCUMENT_ROOT=/data/documents \
    KAIRIX_REFLIB_ROOT=/opt/kairix/reference-library \
    KAIRIX_WORKSPACE_ROOT=/data/kairix/workspaces \
    KAIRIX_DATA_DIR=/data/kairix \
    KAIRIX_CONFIG_PATH=/opt/kairix/kairix.config.yaml

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
