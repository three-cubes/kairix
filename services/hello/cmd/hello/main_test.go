package main

import (
	"bytes"
	"strings"
	"testing"
)

// Table-driven tests for `run` — the testable entrypoint. main() is a
// one-liner around run(), so exercising run() with captured I/O is the
// canonical pattern (no subprocess spawning, no global state).

func TestRun(t *testing.T) {
	tests := []struct {
		name       string
		argv       []string
		wantExit   int
		wantStdout string
		stdoutHas  string
	}{
		{
			name:       "default greeting",
			argv:       []string{},
			wantExit:   0,
			stdoutHas:  "hello kairix — kairix Go pipeline live",
			wantStdout: "",
		},
		{
			name:       "custom name",
			argv:       []string{"--name", "alpha"},
			wantExit:   0,
			stdoutHas:  "hello alpha — kairix Go pipeline live",
			wantStdout: "",
		},
		{
			name:       "version flag short-circuits",
			argv:       []string{"--version"},
			wantExit:   0,
			wantStdout: "hello dev\n",
		},
		{
			name:     "unknown flag returns 2",
			argv:     []string{"--no-such-flag"},
			wantExit: 2,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			got := run(tc.argv, &stdout, &stderr)
			if got != tc.wantExit {
				t.Errorf("exit code: got %d, want %d (stderr=%q)", got, tc.wantExit, stderr.String())
			}
			if tc.wantStdout != "" && stdout.String() != tc.wantStdout {
				t.Errorf("stdout: got %q, want %q", stdout.String(), tc.wantStdout)
			}
			if tc.stdoutHas != "" && !strings.Contains(stdout.String(), tc.stdoutHas) {
				t.Errorf("stdout missing substring: got %q, want substring %q", stdout.String(), tc.stdoutHas)
			}
		})
	}
}

// TestVersionSentinel pins that the default version string is "dev"
// when no -ldflags override happens. Sabotage-proof: changing the
// default to "" or "v0.0.0" would fail this test before any release
// build picks up the wrong sentinel.
func TestVersionSentinel(t *testing.T) {
	if version != "dev" {
		t.Errorf("default version sentinel: got %q, want %q (build-time -ldflags should override at release)", version, "dev")
	}
}

// failWriter is an io.Writer that always returns an error on Write.
// Used to exercise the structured-log error path in run() when the
// greeting write fails (e.g. stdout closed underneath us).
type failWriter struct{}

func (failWriter) Write(_ []byte) (int, error) {
	return 0, errStdoutClosed
}

var errStdoutClosed = &writeError{msg: "stdout closed"}

type writeError struct{ msg string }

func (e *writeError) Error() string { return e.msg }

func TestRunHandlesStdoutWriteFailure(t *testing.T) {
	var stderr bytes.Buffer
	got := run([]string{}, failWriter{}, &stderr)
	if got != 1 {
		t.Errorf("exit code on stdout-write-failure: got %d, want 1 (run() should report and exit non-zero)", got)
	}
	// Structured slog output should mention the error — confirms the
	// log/slog path was taken (G8 enforcement).
	if !strings.Contains(stderr.String(), "stdout closed") {
		t.Errorf("expected structured log to mention the write error, got stderr=%q", stderr.String())
	}
}

func TestRunHandlesVersionWriteFailure(t *testing.T) {
	// Same write-failure shape, but routed through the --version flag
	// branch — exercises the errcheck guard added for golangci-lint v2.
	var stderr bytes.Buffer
	got := run([]string{"--version"}, failWriter{}, &stderr)
	if got != 1 {
		t.Errorf("exit code on --version write-failure: got %d, want 1", got)
	}
	if !strings.Contains(stderr.String(), "stdout closed") {
		t.Errorf("expected structured log to mention the write error, got stderr=%q", stderr.String())
	}
}
