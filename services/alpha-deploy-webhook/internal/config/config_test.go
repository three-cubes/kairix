package config

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// writeSecret creates a file containing val in tmpdir and returns its path.
func writeSecret(t *testing.T, dir, name, val string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(val), 0o600); err != nil {
		t.Fatalf("writeSecret: %v", err)
	}
	return path
}

func TestLoadFromEnvHappyPath(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("WEBHOOK_SECRET_PATH", writeSecret(t, tmp, "secret", "test-secret\n"))
	t.Setenv("KAIRIX_GITHUB_PAT_PATH", writeSecret(t, tmp, "pat", "ghp_test_token"))

	c, err := LoadFromEnv()
	if err != nil {
		t.Fatalf("LoadFromEnv: %v", err)
	}
	if c.Listen != ":9443" {
		t.Errorf("default Listen: got %q, want :9443", c.Listen)
	}
	if c.Repo != "three-cubes/kairix" {
		t.Errorf("default Repo: got %q", c.Repo)
	}
	if string(c.Secret) != "test-secret" {
		t.Errorf("Secret: got %q, want %q (trailing newline must be trimmed)", c.Secret, "test-secret")
	}
	if c.GitHubPAT != "ghp_test_token" {
		t.Errorf("GitHubPAT: got %q", c.GitHubPAT)
	}
	if c.RegressionTolerance != 0.05 {
		t.Errorf("RegressionTolerance default: got %v, want 0.05", c.RegressionTolerance)
	}
}

func TestLoadFromEnvOverrides(t *testing.T) {
	tmp := t.TempDir()
	t.Setenv("WEBHOOK_SECRET_PATH", writeSecret(t, tmp, "secret", "s"))
	t.Setenv("KAIRIX_GITHUB_PAT_PATH", writeSecret(t, tmp, "pat", "p"))
	t.Setenv("WEBHOOK_LISTEN", ":8080")
	t.Setenv("KAIRIX_REPO", "other/repo")
	t.Setenv("KAIRIX_COMPOSE_DIR", "/srv/kairix")
	t.Setenv("KAIRIX_BENCHMARK_SUITE", "custom")
	t.Setenv("KAIRIX_REGRESSION_TOLERANCE", "0.10")

	c, err := LoadFromEnv()
	if err != nil {
		t.Fatalf("LoadFromEnv: %v", err)
	}
	if c.Listen != ":8080" || c.Repo != "other/repo" || c.ComposeDir != "/srv/kairix" || c.BenchmarkSuite != "custom" {
		t.Errorf("overrides not applied: %+v", c)
	}
	if c.RegressionTolerance != 0.10 {
		t.Errorf("RegressionTolerance: got %v, want 0.10", c.RegressionTolerance)
	}
}

func TestLoadFromEnvErrors(t *testing.T) {
	tmp := t.TempDir()
	validSecret := writeSecret(t, tmp, "ok", "x")

	tests := []struct {
		name       string
		setup      func(t *testing.T)
		wantErrSub string
	}{
		{
			name: "missing WEBHOOK_SECRET_PATH",
			setup: func(t *testing.T) {
				t.Setenv("KAIRIX_GITHUB_PAT_PATH", validSecret)
			},
			wantErrSub: "WEBHOOK_SECRET_PATH",
		},
		{
			name: "missing KAIRIX_GITHUB_PAT_PATH",
			setup: func(t *testing.T) {
				t.Setenv("WEBHOOK_SECRET_PATH", validSecret)
			},
			wantErrSub: "KAIRIX_GITHUB_PAT_PATH",
		},
		{
			name: "secret file does not exist",
			setup: func(t *testing.T) {
				t.Setenv("WEBHOOK_SECRET_PATH", filepath.Join(tmp, "no-such-file"))
				t.Setenv("KAIRIX_GITHUB_PAT_PATH", validSecret)
			},
			wantErrSub: "no-such-file",
		},
		{
			name: "secret file is empty",
			setup: func(t *testing.T) {
				t.Setenv("WEBHOOK_SECRET_PATH", writeSecret(t, tmp, "empty", "\n   \n"))
				t.Setenv("KAIRIX_GITHUB_PAT_PATH", validSecret)
			},
			wantErrSub: "is empty",
		},
		{
			name: "regression tolerance not a float",
			setup: func(t *testing.T) {
				t.Setenv("WEBHOOK_SECRET_PATH", validSecret)
				t.Setenv("KAIRIX_GITHUB_PAT_PATH", validSecret)
				t.Setenv("KAIRIX_REGRESSION_TOLERANCE", "not-a-number")
			},
			wantErrSub: "parse KAIRIX_REGRESSION_TOLERANCE",
		},
		{
			name: "regression tolerance negative",
			setup: func(t *testing.T) {
				t.Setenv("WEBHOOK_SECRET_PATH", validSecret)
				t.Setenv("KAIRIX_GITHUB_PAT_PATH", validSecret)
				t.Setenv("KAIRIX_REGRESSION_TOLERANCE", "-0.1")
			},
			wantErrSub: "must be >= 0",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			// Each subtest gets a clean slate; t.Setenv reverts on Cleanup.
			os.Unsetenv("WEBHOOK_SECRET_PATH")
			os.Unsetenv("KAIRIX_GITHUB_PAT_PATH")
			os.Unsetenv("KAIRIX_REGRESSION_TOLERANCE")
			tc.setup(t)
			_, err := LoadFromEnv()
			if err == nil {
				t.Fatalf("expected error containing %q, got nil", tc.wantErrSub)
			}
			if !contains(err.Error(), tc.wantErrSub) {
				t.Errorf("err: got %v, want substring %q", err, tc.wantErrSub)
			}
		})
	}
}

func TestErrMissingRequiredWrapped(t *testing.T) {
	// Sentinel error must be wrapped so callers can errors.Is for typed
	// handling (e.g. "operator forgot to set the env" → exit code 2,
	// not generic 1).
	os.Unsetenv("WEBHOOK_SECRET_PATH")
	os.Unsetenv("KAIRIX_GITHUB_PAT_PATH")
	_, err := LoadFromEnv()
	if !errors.Is(err, ErrMissingRequired) {
		t.Errorf("expected ErrMissingRequired in chain, got %v", err)
	}
}

func contains(haystack, needle string) bool {
	if len(needle) == 0 {
		return true
	}
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
