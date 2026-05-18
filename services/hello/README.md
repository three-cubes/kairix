# hello

Scaffold service that exists only to validate the kairix Go CI pipeline
end-to-end. **Not product code.** Prints a greeting; honours `--version`.

## Why this binary exists

The Phase 1 commit landed the Go pipeline (`.github/workflows/go-quality.yml`,
`.golangci.yml`, Makefile targets, G-rule fitness functions) without any
Go source to run them against. This binary is the smoke test: it proves
gofmt / vet / golangci-lint / test -race -cover / cross-compile all
fire and pass on a real `services/<name>/go.mod`.

When the first real binary (alpha-deploy webhook for
[#272](https://github.com/three-cubes/kairix/issues/272) Phase 4) lands,
this scaffold can be deleted or kept as a permanent "is the pipeline
healthy?" canary. Default: keep, since the operational cost is zero.

## Why this is Go and not Python

Against the four-criterion matrix in
[`docs/architecture/go-integration-plan.md`](../../docs/architecture/go-integration-plan.md):

1. **Outside the Python venv?** Yes — the scaffold's job is to prove a
   non-Python pipeline works.
2. **Single-process, single-binary?** Yes.
3. **Cross-compile required?** Yes — the pipeline ships a 4-target
   matrix; this binary exercises every target.
4. **Small (<2k LOC)?** Yes — ~50 LOC including tests.

Four out of four. Acceptable Go citizen.

## Run

```bash
# Build
make go-build           # builds every services/*/cmd/* target
# or:
cd services/hello && go build -o /tmp/hello ./cmd/hello

# Run
/tmp/hello              # → "hello kairix — kairix Go pipeline live (build dev)"
/tmp/hello --name alpha # → "hello alpha — ..."
/tmp/hello --version    # → "hello dev"
```

## Test

```bash
make go-test            # tests every services/*/go.mod
# or:
cd services/hello && go test -race -cover ./...
```

## Deploy

This binary is not deployed. It exists only in CI. If you ever find
yourself reaching for `services/hello` on a production VM, you've
misread the README — go re-read the Phase 2 section of the integration
plan.
