# Go in the kairix repo — integration plan

> **Status**: plan-of-record. Lands the scaffolding (CI, fitness, conventions, docs) **before** any Go business code. The webhook handler in [#272](https://github.com/three-cubes/kairix/issues/272) Phase 4 is the first user of this pipeline.

## Why a second language at all

Kairix's hot paths are already native (SQLite FTS5, usearch, sentence-transformers, neo4j C driver, spaCy). Python is the glue, which is exactly what Python is good at. **Rewriting glue in Rust earns nothing.**

The single place a second language genuinely earns its keep is **small operational binaries that run outside the Python venv** — webhook handlers, deploy wrappers, log shippers, health probes. Go is the right tool for that slot:

- single static binary; no venv, no `pip install` on the host
- builtin HTTP server; HMAC verification is stdlib
- cross-compile from any laptop to linux/amd64 or linux/arm64
- ops surface readable by anyone with `go fmt`/`go vet` baseline knowledge

The case is *not* "Go is faster than Python" — the case is "this code shouldn't be Python in the first place because it runs in a context where Python adds packaging and runtime cost without buying us anything."

## Hard rules

1. **Go is only for ops binaries.** Anything that touches retrieval, agents, eval, MCP, or domain logic stays in Python. The Python decision-matrix in [ENGINEERING.md](ENGINEERING.md) §11 governs whether a Python rewrite is appropriate — Go is not a substitute for that decision.
2. **Every Go binary must justify itself against the "could this be Python" question.** Add a one-paragraph rationale in the binary's `README.md`. The default answer is "use Python."
3. **No multi-language services.** A binary is wholly Go or wholly Python. No FFI, no PyO3, no embedded Python. (Rust+PyO3 is a separate future discussion — not in scope for this plan.)
4. **Go binaries are operational, not core product.** If a Go binary disappeared overnight, kairix retrieval would still work. The Go binaries glue ops together; they don't host product surface.
5. **No new operator-host Python dependencies.** Where bash works today on Linux VMs (`apply-kairix-config.sh` etc), bash stays. Go replaces bash only when cross-platform matters.

## Repo layout

```
services/                              # Operational Go binaries
  <service-name>/
    cmd/<service-name>/main.go          # entrypoint (Go convention: cmd/<name>)
    internal/                           # service-private packages
    go.mod
    go.sum
    README.md                           # what + why + run + deploy
tools/                                  # optional later — single-file Go utilities
```

Why `services/` rather than `cmd/` at the repo root:
- Repo root `cmd/` is the Go convention for a single-module repo where all binaries share dependencies. We don't have that — Python is the primary module.
- `services/<name>/go.mod` per binary keeps dependency surface scoped per binary; one binary's `golang.org/x/crypto` bump doesn't drag the others.
- Mirrors the `kairix/<package>/` Python layout — readers expect topic dirs at the root.

## CI: parallel pipeline, isolated by path filter

Add `.github/workflows/go-quality.yml` triggered on push and PR when `services/**` or `tools/**` changes. The workflow runs:

| Stage | Tool | Floor |
|---|---|---|
| Format | `gofmt -l` (must produce no output) | Zero diff |
| Vet | `go vet ./...` | Zero findings |
| Lint | `golangci-lint run` with `.golangci.yml` from repo root | Zero findings |
| Test | `go test ./... -race -cover -covermode=atomic` | ≥ 80% per-binary coverage (ratchets to 90% as the F7-equivalent matures) |
| Build matrix | `linux/amd64`, `linux/arm64`, `darwin/amd64`, `darwin/arm64` | All targets compile |

Per-binary CI works by discovering each `services/*/go.mod` and running the gate inside that directory. No top-level Go module.

The Python `1 · Quality gate` workflow is untouched. The two are independent — Python green doesn't depend on Go green and vice versa. The branch-protection `CI gate` aggregator becomes a fan-in that requires both.

## Architecture fitness — extending F1-F24 to Go

Python F-rules don't translate verbatim. Go has different idioms; some Python rules vanish (Go has no `monkeypatch`), some need translation (testing markers → build tags), some are new (context propagation, error wrapping). The Go-side rules:

| # | Rule | What |
|---|---|---|
| **G1** | Every `services/*/cmd/<name>/main.go` exposes `--version` | Operator-visible version surface; emit setuptools-scm-equivalent build-time ldflag. |
| **G2** | Errors wrap with `%w` | `fmt.Errorf("operation: %w", err)` not `fmt.Errorf("operation: %s", err)`. AST/regex check. |
| **G3** | No `interface{}` / `any` in exported signatures | Generics or named interface types only. Boundary exception: JSON unmarshal staging structs. |
| **G4** | Context.Context as first arg on every exported function that does I/O | `func Do(ctx context.Context, ...) error`. Detector: any function calling `http.*` / `os.Open` / `exec.*` without a `context.Context` parameter is flagged. |
| **G5** | Every Go package has a doc comment | `// Package foo …` on the package-declaration line. |
| **G6** | No `panic` in non-`main` packages | `panic` allowed only in `cmd/<name>/main.go` and in `init()` for unrecoverable startup failures. |
| **G7** | Tests follow Go convention | `*_test.go` siblings of the file under test; `TestXxx(t *testing.T)` only; no `package <name>_test` mixed with `package <name>` in the same directory. |
| **G8** | Logging via `log/slog` only | No `fmt.Println` / `log.Printf` in production code. `slog.Info(...)`. Required for structured ops logs. |
| **G9** | Every `services/<name>/` has a `README.md` | Mirrors F23. Explains what+why+run+deploy. |
| **G10** | No external dependencies without rationale | Each entry in `services/<name>/go.mod` requires a `// reason: ...` comment in a `services/<name>/DEPENDENCIES.md` registry. Matches our Python `pyproject.toml` rationale pattern. |

Pre-existing violations grandfathered in `.architecture/baseline/go-<rule>-files.txt` (same pattern as F1-F24).

Detection scripts live in `scripts/checks/check_go_*.{py,sh}`. They walk `services/**` with `go/ast` (via shelling out to a small Go helper) or with `gofmt -d`-style probes. The Python F-rule pattern (`gate()` from `_arch_lib`) is reused for orchestration so failures still emit the universal affordance template.

## Standards — committed conventions

Code style:
- `gofmt -s` (with simplify) is canonical. No exceptions.
- `golangci-lint` config at repo root `.golangci.yml`. Enabled linters: `errcheck`, `govet`, `staticcheck`, `unused`, `gosimple`, `ineffassign`, `gocritic`, `errorlint`, `bodyclose`, `gosec`, `revive`. (Generated config below.)
- Line length: gofmt decides. We do not impose a column limit.

Module conventions:
- Module path: `github.com/three-cubes/kairix/services/<name>`. Even for internal-only binaries — gives us a stable import path and matches Go module conventions.
- Go version: pin to the latest stable minor in `go.mod` (`go 1.23` at the time of writing). Bump on schedule, not opportunistically.
- Dependencies: minimal. Standard library first. Each third-party dep requires a one-line rationale in `services/<name>/DEPENDENCIES.md`.

Error handling:
- Sentinel errors via `errors.Is`. Wrap with `%w` always. Return paths use `errors.As` for type-narrowing. No `panic` outside `main`.

Testing:
- Table-driven tests by default. `testing.T.Run(name, …)` for subtests.
- `testify` is **not** allowed unless explicitly justified — vanilla `testing` package is the rule. (We want fewer dependencies, not more.)
- Race detector on every test run (`go test -race`).
- Coverage gate per-binary; ratchet matches F7's evolution (start at 80%, climb to 90%).

Logging:
- `log/slog` with `slog.NewJSONHandler` for ops binaries. Always structured. Always machine-parseable.
- Log levels: DEBUG/INFO/WARN/ERROR. No custom levels.

Concurrency:
- `context.Context` propagation through every I/O call.
- Goroutines are owned — every `go f()` has an explicit `sync.WaitGroup` or channel-based join. No fire-and-forget.

Deployment:
- Binaries cross-compiled in CI. Release artifacts are uploaded as GitHub Release assets with SHA-256 sums.
- On the VM: place binary at `/opt/kairix/bin/<name>` with `0755` mode. Systemd unit if long-running; cron entry if periodic.
- Version embedded via `-ldflags "-X main.version=<tag>"` at build time.

Security:
- `gosec` in the lint pipeline. Zero HIGH/MEDIUM findings.
- Secrets via env vars or files at well-known paths (`/run/secrets/...`). Never in source, never in command-line args (visible in `ps`).
- Webhook signature verification is constant-time (`crypto/hmac` `Equal`). No `bytes.Equal`.

## Build sequence (this plan's own work items)

**Phase 1 — scaffolding (this commit batch, no Go business code)**
- [x] Plan doc (this file).
- [ ] `.golangci.yml` at repo root.
- [ ] `.github/workflows/go-quality.yml` — full pipeline.
- [ ] `services/README.md` — explains the convention and the per-service-module layout.
- [ ] First fitness function script: `scripts/checks/check_go_readme_coverage.py` (G9 — Python-side check that every `services/<name>/` has a README).
- [ ] First baseline file: `.architecture/baseline/go-readme-coverage-files.txt` (empty).
- [ ] Makefile additions: `go-fmt`, `go-vet`, `go-lint`, `go-test`, `go-build`.
- [ ] CLAUDE.md update: Go section pointing at this plan.
- [ ] ENGINEERING.md §11: cross-link to this plan + the language-choice decision matrix.

**Phase 2 — proof-of-pipeline (a tiny scaffold service that exists only to verify the gates work)**
- [ ] `services/hello/cmd/hello/main.go` — emits `hello v<version>`.
- [ ] `services/hello/cmd/hello/main_test.go` — one passing test.
- [ ] `services/hello/go.mod` + `services/hello/README.md` + `services/hello/DEPENDENCIES.md`.
- [ ] CI pass: `go fmt` clean, `go vet` clean, `golangci-lint` clean, `go test -race -cover` clean, build matrix all four targets compile.

**Phase 3 — first real binary (kicks off [#272](https://github.com/three-cubes/kairix/issues/272) Phase 4)**
- [ ] `services/alpha-deploy-webhook/` — receives signed POSTs from `release-vm-deploy.yml`, pulls alpha Docker image, runs onboard check + reflib benchmark, posts back via GitHub commit status.
- [ ] Deploy to VM via systemd unit. Cross-repo: your sibling infrastructure repo owns the deploy mechanics (e.g. a separate ops repo with systemd units + apply scripts); this repo owns the binary.

**Phase 4 — additional fitness functions (rolled in as the Go surface grows)**
- [ ] G1 (`--version` flag), G2 (error wrap), G6 (no `panic` outside main), G8 (`log/slog`), G10 (dependency rationale).
- [ ] Each lands with a sabotage-proven test and a baseline file. Baselines stay empty by construction since we control every Go file from day one.

## What this plan does NOT commit to

- **Rust or PyO3 anywhere.** Out of scope.
- **Rewriting any Python in Go.** Go is for binaries that don't exist yet in Python.
- **A top-level repo Go module.** Per-service modules; no monorepo Go-side dependency mixing.
- **TypeScript / web UI.** Not relevant to kairix's current scope.

## Decision criteria for future Go binaries

Before any new `services/<name>/` lands, justify against:

1. **Does this need to run outside the Python venv?** (e.g. on a host without `pip`, in a container without Python.) If no, use Python.
2. **Is it single-process, single-binary?** Multi-process is Python's strength (subprocess management, IPC). If your design needs process orchestration, use Python.
3. **Does it need cross-compilation?** If everyone runs on the same Linux distro and same Python version, no benefit.
4. **Is it small (<2k LOC)?** Above this, Go's "no real package management" friction starts to bite for a single-language team. Reconsider whether the operational surface should grow that large.

If you can't tick at least two of those four, the answer is Python.

## Update cadence

Review this plan whenever a new Go binary is proposed. If the criteria above prove insufficient or wrong, update the plan first, then write the code. The plan precedes implementation.
