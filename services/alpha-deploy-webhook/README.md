# alpha-deploy-webhook

VM-side handler for the kairix alpha-deploy regression gate (#272 Phase 4).
GitHub Actions POSTs a signed deploy request; this binary pulls the alpha
Docker image, runs `kairix onboard check --json` and `kairix benchmark run
--suite reflib`, then posts the result back as a GitHub commit status on
the alpha tag's SHA.

## Why Go, not Python (per `docs/architecture/go-integration-plan.md`)

Four-of-four against the decision criteria:

1. **Outside Python venv** — runs on a VM that already has Python for the kairix
   container itself; adding webhook code to that venv mixes concerns.
2. **Single-process, single-binary** — one HTTP server, stdlib-only.
3. **Cross-compile required** — built in CI for linux/arm64 (the VM
   architecture); operator never sees a `go install` step.
4. **Small (~500 LOC)** — well within the operational-binary slot.

The pull-based webhook pattern is **deliberately** chosen over an SSH
deploy key in GitHub Actions: the VM holds the trust material (HMAC
secret + scoped GitHub PAT), not Actions. SSH key in Actions has shell-
level blast radius; HMAC-only-with-PAT-on-VM has narrow, rotatable trust.

## Endpoints

- `POST /deploy` — request body: `{"version": "v2026.5.15a1", "sha":
  "<commit-sha>", "callback_run_id": "<gha-run-id>"}`. Header
  `X-Kairix-Signature: sha256=<hex>` with HMAC-SHA256 of the raw
  request body using the shared secret.
- `GET /healthz` — liveness only; returns 200 with `{"ok": true}`.

## Configuration (env vars)

| Var | Required | Purpose |
|---|---|---|
| `WEBHOOK_SECRET_PATH` | yes | File containing HMAC shared secret (read once at boot; constant-time compare on each request) |
| `WEBHOOK_LISTEN` | no (default `:9443`) | `host:port` to listen on |
| `KAIRIX_GITHUB_PAT_PATH` | yes | File containing GitHub PAT with `repo:status` scope (read once at boot) |
| `KAIRIX_REPO` | no (default `three-cubes/kairix`) | owner/repo for commit-status POSTs |
| `KAIRIX_COMPOSE_DIR` | no (default `/opt/kairix/app`) | docker-compose directory |
| `KAIRIX_BENCHMARK_SUITE` | no (default `reflib`) | benchmark suite to run for regression gate |
| `KAIRIX_REGRESSION_TOLERANCE` | no (default `0.05`) | weighted_total delta allowed below baseline |

Secrets are read from files (not env vars directly) so that `ps`-visible
process state never carries them. The systemd unit can mount
`/run/secrets/` and point the *_PATH vars there.

## Deploy

Built in CI by `.github/workflows/go-quality.yml` (build-matrix
linux/arm64). Distributed via the same release pipeline as the docker
image. systemd unit + apply script live in `tc-agent-zone` (cross-repo;
see kairix#272 Phase 4 follow-ups).

## Run locally

```bash
cd services/alpha-deploy-webhook
go test -race -cover ./...
# build
go build -o /tmp/alpha-deploy-webhook ./cmd/alpha-deploy-webhook
# run (will fail without secrets — see Configuration above)
WEBHOOK_SECRET_PATH=/tmp/secret KAIRIX_GITHUB_PAT_PATH=/tmp/pat /tmp/alpha-deploy-webhook --version
```

## Operational notes

- Listens behind `cloudflared` on the VM — no public network exposure.
- Each `/deploy` request is fire-and-async: the handler returns
  `202 Accepted` immediately after signature verify, then runs the
  pull+check+benchmark cycle in a background goroutine. The commit-
  status POST is the visible signal; GitHub Actions polls that status
  to detect completion. This avoids holding the HTTP connection open
  for the ~5min benchmark.
- Logs go to stderr as JSON via `log/slog` (G8). systemd captures them.
