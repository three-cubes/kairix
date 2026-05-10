# ── Build stage: install all dependencies ────────────────────────────────────
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# NOSONAR(docker:S6470): the `setup.cfg* setup.py*` globs are bounded to
# specific filenames at the repo root — they only pick up legitimate Python
# package metadata, never secrets. `.dockerignore` (root of repo) excludes
# anything sensitive (.env, .git, secrets/) before context is sent to the
# builder. Reviewed and safe.
COPY pyproject.toml setup.cfg* setup.py* README.md /opt/kairix/src/
COPY kairix/ /opt/kairix/src/kairix/

# Install PyTorch CPU-only first (prevents pulling ~5GB CUDA libs on GPU-less servers)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir "/opt/kairix/src[neo4j,agents,nlp,rerank]" \
    && python -m spacy download en_core_web_sm || true

# ── Runtime stage: slim image with only installed packages ───────────────────
# NOSONAR(docker:S6504): kairix runs as root inside the container by design —
# it needs to write to /data/kairix/ and bind-mounted document roots whose
# host ownership varies per deployment. Operators isolate the container with
# user-namespace remapping or a non-root host user via docker-compose `user:`
# rather than baking a fixed UID into the image. Reviewed and safe.
FROM python:3.12-slim AS runtime

# Copy installed Python packages from builder (no build-essential, no source)
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Create runtime directories and install runtime-only system deps (curl for healthchecks)
RUN mkdir -p /data/documents /data/kairix /data/kairix/workspaces /opt/kairix/bin /opt/kairix/cron \
    && apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

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
