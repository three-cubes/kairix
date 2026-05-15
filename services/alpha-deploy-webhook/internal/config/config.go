// Package config parses operational configuration from the environment
// and from secret files. Secrets are NEVER passed via env-var bytes —
// only the *path* to a file containing the secret is parsed from the
// env. This keeps process command-line / `ps`-visible state secret-free
// and matches the kairix /run/secrets pattern.
package config

import (
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// Config bundles every runtime knob. Constructed once at startup from
// LoadFromEnv. Tests construct directly; production calls LoadFromEnv.
type Config struct {
	// Listen is the host:port the HTTP server binds to.
	Listen string
	// Secret is the HMAC shared secret bytes (loaded from SecretPath).
	Secret []byte
	// GitHubPAT is the bearer token used for commit-status POSTs
	// (loaded from PATPath). Scope must be `repo:status`.
	GitHubPAT string
	// Repo is the owner/repo string for GitHub commit-status API calls.
	Repo string
	// ComposeDir is the docker-compose directory on the host.
	ComposeDir string
	// BenchmarkSuite is the suite name passed to `kairix benchmark run`.
	BenchmarkSuite string
	// RegressionTolerance is the maximum allowed delta below baseline
	// for the weighted_total benchmark metric.
	RegressionTolerance float64
}

// ErrMissingRequired is returned when a required env var is unset or
// points at a non-existent / empty file.
var ErrMissingRequired = errors.New("required configuration is missing")

// LoadFromEnv populates Config from environment variables. Documented
// in services/alpha-deploy-webhook/README.md §Configuration.
func LoadFromEnv() (*Config, error) {
	c := &Config{
		Listen:              envOr("WEBHOOK_LISTEN", ":9443"),
		Repo:                envOr("KAIRIX_REPO", "three-cubes/kairix"),
		ComposeDir:          envOr("KAIRIX_COMPOSE_DIR", "/opt/kairix/app"),
		BenchmarkSuite:      envOr("KAIRIX_BENCHMARK_SUITE", "reflib"),
		RegressionTolerance: 0.05,
	}

	if v := os.Getenv("KAIRIX_REGRESSION_TOLERANCE"); v != "" {
		f, err := strconv.ParseFloat(v, 64)
		if err != nil {
			return nil, fmt.Errorf("parse KAIRIX_REGRESSION_TOLERANCE %q: %w", v, err)
		}
		if f < 0 {
			return nil, fmt.Errorf("KAIRIX_REGRESSION_TOLERANCE must be >= 0, got %v", f)
		}
		c.RegressionTolerance = f
	}

	secretPath := os.Getenv("WEBHOOK_SECRET_PATH")
	if secretPath == "" {
		return nil, fmt.Errorf("%w: WEBHOOK_SECRET_PATH", ErrMissingRequired)
	}
	secret, err := readSecret(secretPath)
	if err != nil {
		return nil, fmt.Errorf("read WEBHOOK_SECRET_PATH %q: %w", secretPath, err)
	}
	c.Secret = secret // pragma: allowlist secret — struct field assignment of read-from-file bytes, not a hardcoded value

	patPath := os.Getenv("KAIRIX_GITHUB_PAT_PATH")
	if patPath == "" {
		return nil, fmt.Errorf("%w: KAIRIX_GITHUB_PAT_PATH", ErrMissingRequired)
	}
	patBytes, err := readSecret(patPath)
	if err != nil {
		return nil, fmt.Errorf("read KAIRIX_GITHUB_PAT_PATH %q: %w", patPath, err)
	}
	c.GitHubPAT = string(patBytes)

	return c, nil
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

// readSecret reads a single-secret file, trimming trailing whitespace
// (operators sometimes leave a stray newline). Empty file is an error
// — that's a misconfiguration we want loud, not a silent empty secret.
func readSecret(path string) ([]byte, error) {
	raw, err := os.ReadFile(path) // #nosec G304 G703 — path is operator-supplied env var (WEBHOOK_SECRET_PATH / KAIRIX_GITHUB_PAT_PATH), validated upstream; deliberate.
	if err != nil {
		return nil, err
	}
	trimmed := []byte(strings.TrimRight(string(raw), " \t\r\n"))
	if len(trimmed) == 0 {
		return nil, fmt.Errorf("%w: file is empty", ErrMissingRequired)
	}
	return trimmed, nil
}
