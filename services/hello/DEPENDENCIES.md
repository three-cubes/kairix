# Dependency rationale (G10)

Every third-party Go module in `go.mod` requires a one-line rationale
here. Stdlib imports are exempt.

## Current dependencies

_None._ This binary is stdlib-only — `flag`, `fmt`, `io`, `log/slog`, `os`.

## Adding a new dependency

1. Justify against the "could stdlib do this?" question first.
2. If yes, add the rationale here as one line: `module — reason`.
3. Run `go mod tidy` in the service directory.
4. Commit `go.mod`, `go.sum`, and this file in the same change.
