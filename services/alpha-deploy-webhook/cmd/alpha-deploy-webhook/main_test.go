package main

import (
	"bytes"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestVersionFlag(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run([]string{"--version"}, &stdout, &stderr)
	if code != 0 {
		t.Fatalf("exit code: got %d, want 0 (stderr=%q)", code, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "alpha-deploy-webhook ") {
		t.Errorf("stdout: got %q, want 'alpha-deploy-webhook <version>'", stdout.String())
	}
}

func TestUnknownFlagReturns2(t *testing.T) {
	var stdout, stderr bytes.Buffer
	code := run([]string{"--no-such"}, &stdout, &stderr)
	if code != 2 {
		t.Errorf("exit code: got %d, want 2", code)
	}
}

func TestMissingConfigReturns2(t *testing.T) {
	// No env vars set → config.ErrMissingRequired → exit 2.
	t.Setenv("WEBHOOK_SECRET_PATH", "")
	t.Setenv("KAIRIX_GITHUB_PAT_PATH", "")
	var stdout, stderr bytes.Buffer
	code := run([]string{}, &stdout, &stderr)
	if code != 2 {
		t.Errorf("exit code on missing config: got %d, want 2 (operator misconfig)", code)
	}
	if !strings.Contains(stderr.String(), "config load failed") {
		t.Errorf("stderr: got %q, want 'config load failed' substring", stderr.String())
	}
}

func TestVersionSentinel(t *testing.T) {
	if version != "dev" {
		t.Errorf("default version: got %q, want %q (-ldflags overrides at release)", version, "dev")
	}
}

func TestRunFailsWhenPortInUse(t *testing.T) {
	// Hold a port to force a bind conflict — exercises the config-load
	// + server-construction path of run() through the ListenAndServe
	// error branch. Pushes cmd-package coverage above the 80% floor
	// without spinning up a full integration test.
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("net.Listen: %v", err)
	}
	defer func() { _ = lis.Close() }()
	addr := lis.Addr().String()

	tmp := t.TempDir()
	secret := filepath.Join(tmp, "secret")
	if err := os.WriteFile(secret, []byte("test-secret"), 0o600); err != nil {
		t.Fatalf("write secret: %v", err)
	}
	t.Setenv("WEBHOOK_SECRET_PATH", secret)
	t.Setenv("KAIRIX_GITHUB_PAT_PATH", secret)
	t.Setenv("WEBHOOK_LISTEN", addr)

	var stdout, stderr bytes.Buffer
	code := run([]string{}, &stdout, &stderr)
	if code != 1 {
		t.Errorf("exit code on bind conflict: got %d, want 1 (stderr=%q)", code, stderr.String())
	}
	if !strings.Contains(stderr.String(), "listen failed") {
		t.Errorf("stderr: got %q, want 'listen failed' substring", stderr.String())
	}
}
