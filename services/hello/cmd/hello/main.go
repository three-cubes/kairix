// Package main is the kairix hello binary — a scaffold service that
// exists only to validate the Go CI pipeline (gofmt, vet, golangci-lint,
// test -race -cover, cross-compile matrix) end-to-end.
//
// Pairs with docs/architecture/go-integration-plan.md Phase 2. Real
// binaries (alpha-deploy webhook for #272 Phase 4 and onwards) reuse
// the same Phase 2 cmd-package layout demonstrated here.
package main

import (
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
)

// version is overridden at build time via:
//
//	go build -ldflags "-X main.version=$(git describe --tags)" ./cmd/hello
//
// CI sets it from the release tag. Local builds default to "dev".
var version = "dev"

func main() {
	exitCode := run(os.Args[1:], os.Stdout, os.Stderr)
	os.Exit(exitCode)
}

// run is the testable entrypoint — main thin-wraps it so unit tests
// can pass argv + capture stdout/stderr without spawning a process.
// Returns the process exit code; callers in main() pass to os.Exit.
func run(argv []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("hello", flag.ContinueOnError)
	fs.SetOutput(stderr)
	showVersion := fs.Bool("version", false, "print version and exit (G1)")
	name := fs.String("name", "kairix", "name to greet")

	if err := fs.Parse(argv); err != nil {
		// flag.ContinueOnError already wrote a usage message to stderr.
		return 2
	}

	logger := slog.New(slog.NewJSONHandler(stderr, nil)) // G8: structured logs

	if *showVersion {
		if _, err := fmt.Fprintf(stdout, "hello %s\n", version); err != nil {
			logger.Error("write version", slog.String("err", err.Error()))
			return 1
		}
		return 0
	}

	if _, err := fmt.Fprintf(stdout, "hello %s — kairix Go pipeline live (build %s)\n", *name, version); err != nil {
		logger.Error("write greeting", slog.String("err", err.Error()))
		return 1
	}
	return 0
}
