# Dependency rationale (G10)

## Current dependencies

_None._ stdlib-only:

- `context` — request scoping + cancellation
- `crypto/hmac` + `crypto/sha256` + `crypto/subtle` — constant-time HMAC verify
- `encoding/hex` — signature header parsing
- `encoding/json` — request/response bodies + GitHub API
- `errors` — wrapping
- `flag` — `--version` plus minimal CLI flags
- `fmt` — error wrapping (`Errorf`)
- `io` — request body read
- `log/slog` — structured logging (G8)
- `net/http` — HTTP server + GitHub API POST
- `os` + `os/exec` — env vars + docker compose / kairix CLI exec
- `strings` — small parsing helpers
- `testing` — vanilla unit tests
- `time` — request timeouts

No third-party modules. If a future feature needs one (e.g. structured
GitHub API client), add the rationale line here BEFORE running
`go get`. The G10 fitness function fails the gate otherwise.
