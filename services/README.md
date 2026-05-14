# services/

Operational Go binaries. **Not core kairix product surface** — the Python
package under `kairix/` owns retrieval, agents, eval, and MCP. Binaries here
are the operational glue that runs *outside* the Python venv: webhook
handlers, deploy wrappers, log shippers, health probes.

See [`docs/architecture/go-integration-plan.md`](../docs/architecture/go-integration-plan.md)
for the full rationale, hard rules, and decision criteria for whether a new
binary belongs here at all. **Default answer to "should this be Go?" is no.**

## Layout per binary

```
services/<name>/
  cmd/<name>/main.go         # entrypoint (single binary per directory)
  internal/                  # service-private packages
  go.mod                     # per-service module — no shared top-level go.mod
  go.sum
  DEPENDENCIES.md            # one-line rationale per third-party import (G10)
  README.md                  # what + why + run + deploy
```

The per-service `go.mod` is deliberate: a `golang.org/x/crypto` bump in the
webhook handler does not drag the log shipper along. Matches the
operational-binary philosophy — each binary is independently versioned and
independently deployed.

## How to add a service

1. Read the integration plan first. Tick at least two of the four decision
   criteria in §"Decision criteria for future Go binaries". Default answer
   is Python; Go has to earn its slot.
2. Create `services/<name>/cmd/<name>/main.go` + `go.mod` + `README.md` +
   `DEPENDENCIES.md`.
3. The Go-quality workflow auto-discovers via `find services -mindepth 2
   -maxdepth 2 -name go.mod` — no workflow edit needed.
4. CI runs `gofmt -s`, `go vet`, `golangci-lint`, `go test -race -cover`,
   and cross-compile to linux/amd64, linux/arm64, darwin/amd64, darwin/arm64.
5. Coverage floor is 80% per-binary at landing; ratchets to 90% (mirrors F7).

## Currently shipped

> Nothing yet. The infrastructure (CI, lint config, fitness functions,
> conventions) lands first; the first real binary is the
> alpha-deploy webhook for [#272](https://github.com/three-cubes/kairix/issues/272)
> Phase 4.
