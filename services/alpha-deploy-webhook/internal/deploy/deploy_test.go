package deploy

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"testing"
)

// fakeRunner is a test-only Runner that returns scripted outputs per
// (cmd, joined-args) key. Unknown commands return an error so missing
// fixtures surface as test failures, not silent passes.
type fakeRunner struct {
	responses map[string]fakeResponse
	calls     []string
}

type fakeResponse struct {
	out []byte
	err error
}

func newFakeRunner(responses map[string]fakeResponse) *fakeRunner {
	return &fakeRunner{responses: responses}
}

func (f *fakeRunner) Run(_ context.Context, _, cmd string, args ...string) ([]byte, error) {
	key := cmd + " " + strings.Join(args, " ")
	f.calls = append(f.calls, key)
	r, ok := f.responses[key]
	if !ok {
		return nil, fmt.Errorf("fakeRunner: no fixture for %q", key)
	}
	return r.out, r.err
}

func newSilentLogger() *slog.Logger {
	return slog.New(slog.NewJSONHandler(io.Discard, nil))
}

func benchmarkOutput(weighted float64) []byte {
	return []byte(fmt.Sprintf(`Suite: reflib  (200 cases)
============================================================
BENCHMARK RESULTS
============================================================
Weighted total: %.3f  [Phase 4 target]
NDCG@10:       0.947
`, weighted))
}

func onboardOK() []byte  { return []byte(`{"passed":9,"total":9,"fully_passed":true,"failures":[]}`) }
func onboardBad() []byte { return []byte(`{"passed":7,"total":9,"fully_passed":false,"failures":[]}`) }

func TestServiceRunHappyPath(t *testing.T) {
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: benchmarkOutput(0.889)},
	})
	s := &Service{
		Runner:                r,
		ComposeDir:            "/opt/kairix/app",
		BenchmarkSuite:        "reflib",
		RegressionTolerance:   0.05,
		BaselineWeightedTotal: 0.901,
		Logger:                newSilentLogger(),
	}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if !got.Success {
		t.Errorf("Success: got false (summary=%q, details=%q), want true", got.Summary, got.Details)
	}
	if got.WeightedTotal != 0.889 {
		t.Errorf("WeightedTotal: got %v, want 0.889", got.WeightedTotal)
	}
}

func TestServiceRunRegressionExceedsTolerance(t *testing.T) {
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: benchmarkOutput(0.800)},
	})
	s := &Service{
		Runner:                r,
		ComposeDir:            "/opt/kairix/app",
		BenchmarkSuite:        "reflib",
		RegressionTolerance:   0.05,
		BaselineWeightedTotal: 0.901,
		Logger:                newSilentLogger(),
	}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if got.Success {
		t.Errorf("Success: got true, want false (0.901-0.800=0.101 > 0.05 tolerance)")
	}
	if !strings.Contains(got.Summary, "regression") {
		t.Errorf("Summary: got %q, want 'regression' substring", got.Summary)
	}
}

func TestServiceRunOnboardFailure(t *testing.T) {
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardBad()},
	})
	s := &Service{Runner: r, ComposeDir: "/opt/kairix/app", BenchmarkSuite: "reflib", Logger: newSilentLogger()}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if got.Success {
		t.Errorf("Success: got true, want false")
	}
	if !strings.Contains(got.Summary, "onboard check failed") {
		t.Errorf("Summary: got %q, want 'onboard check failed' substring", got.Summary)
	}
}

func TestServiceRunPullFailure(t *testing.T) {
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker": {out: []byte("error"), err: errors.New("network down")},
	})
	s := &Service{Runner: r, ComposeDir: "/opt/kairix/app", Logger: newSilentLogger()}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if got.Success {
		t.Errorf("Success: got true, want false (pull failure should short-circuit)")
	}
	if !strings.Contains(got.Summary, "docker pull/up failed") {
		t.Errorf("Summary: got %q, want 'docker pull/up failed' substring", got.Summary)
	}
}

func TestServiceRunRefreshSecretsCalledBeforeCompose(t *testing.T) {
	// Pin the ordering of the deploy sequence: systemctl restart
	// kairix-fetch-secrets MUST run before docker compose pull so the
	// container picks up a fresh /run/secrets/kairix.env when Key Vault
	// has rotated. Sabotage: drop the refreshSecrets call from Run() and
	// the first recorded call becomes the compose pull, breaking the
	// strict-order assertion below.
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: benchmarkOutput(0.889)},
	})
	s := &Service{
		Runner:                r,
		ComposeDir:            "/opt/kairix/app",
		BenchmarkSuite:        "reflib",
		RegressionTolerance:   0.05,
		BaselineWeightedTotal: 0.901,
		Logger:                newSilentLogger(),
	}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if !got.Success {
		t.Errorf("Success: got false (summary=%q, details=%q), want true", got.Summary, got.Details)
	}
	if len(r.calls) == 0 || !strings.HasPrefix(r.calls[0], "systemctl restart kairix-fetch-secrets.service") {
		t.Errorf("first call should be systemctl restart kairix-fetch-secrets; got calls=%v", r.calls)
	}
}

func TestServiceRunContinuesWhenFetchSecretsUnitMissing(t *testing.T) {
	// Dev / first-time hosts may not have kairix-fetch-secrets.service
	// installed. Deploy should log + continue, not hard-fail. The
	// subsequent onboard check is where a real misconfiguration should
	// halt the deploy, not at the secrets-refresh prologue.
	// Sabotage: change refreshSecrets to return the systemctl error
	// instead of nil and this test fails because the deploy stops at
	// "secrets refresh failed" instead of continuing.
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Unit not found"), err: errors.New("exit 5")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: benchmarkOutput(0.889)},
	})
	s := &Service{
		Runner:                r,
		ComposeDir:            "/opt/kairix/app",
		BenchmarkSuite:        "reflib",
		RegressionTolerance:   0.05,
		BaselineWeightedTotal: 0.901,
		Logger:                newSilentLogger(),
	}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if !got.Success {
		t.Errorf("Success: got false (summary=%q, details=%q), want true — fetch-secrets failure should not halt deploy", got.Summary, got.Details)
	}
}

func TestServiceRunBenchmarkParseFailure(t *testing.T) {
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: []byte("no parseable output")},
	})
	s := &Service{Runner: r, ComposeDir: "/opt/kairix/app", BenchmarkSuite: "reflib", Logger: newSilentLogger()}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if got.Success {
		t.Errorf("Success: got true, want false (unparseable benchmark)")
	}
	if !strings.Contains(got.Summary, "benchmark failed") {
		t.Errorf("Summary: got %q, want 'benchmark failed' substring", got.Summary)
	}
}

func TestServiceRunComposeUpWaitsForHealthcheck(t *testing.T) {
	// Pins the deploy contract that docker compose up blocks on the
	// container's healthcheck before the webhook's own onboard check
	// fires. Without --wait, the webhook races the MCP server warm-up +
	// port bind and the onboard check fails on a freshly recreated
	// container (observed every deploy from v2026.5.16a4 through
	// v2026.5.17a5 — webhook-side "deploy failure" while the container
	// itself was healthy seconds later).
	//
	// Sabotage: drop --wait (or --wait-timeout) from the up command in
	// pullAndUp and the fakeRunner key stops matching, failing the deploy.
	r := newFakeRunner(map[string]fakeResponse{
		"systemctl restart kairix-fetch-secrets.service": {out: []byte("Started")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml pull kairix kairix-worker":                                            {out: []byte("Pulled")},
		"sh -c KAIRIX_IMAGE_TAG='2026.5.15a1' docker compose -f docker-compose.yml -f docker-compose.override.yml up -d --force-recreate --wait --wait-timeout 90 kairix kairix-worker": {out: []byte("Started")},
		"docker exec app-kairix-1 sh -c kairix onboard check --json 2>/dev/null":                                                                                                        {out: onboardOK()},
		"docker exec app-kairix-1 sh -c cd /opt/kairix && kairix benchmark run --suite reflib":                                                                                          {out: benchmarkOutput(0.889)},
	})
	s := &Service{
		Runner:                r,
		ComposeDir:            "/opt/kairix/app",
		BenchmarkSuite:        "reflib",
		RegressionTolerance:   0.05,
		BaselineWeightedTotal: 0.901,
		Logger:                newSilentLogger(),
	}
	got := s.Run(context.Background(), "v2026.5.15a1")
	if !got.Success {
		t.Errorf("Success: got false (summary=%q, details=%q), want true", got.Summary, got.Details)
	}
	upCalls := []string{}
	for _, c := range r.calls {
		if strings.Contains(c, "docker compose") && strings.Contains(c, "up -d") {
			upCalls = append(upCalls, c)
		}
	}
	if len(upCalls) != 1 {
		t.Fatalf("expected exactly one 'docker compose up -d' call; got %d: %v", len(upCalls), upCalls)
	}
	if !strings.Contains(upCalls[0], "--wait") {
		t.Errorf("expected --wait in compose up command; got: %s", upCalls[0])
	}
}

func TestParseWeightedTotal(t *testing.T) {
	tests := []struct {
		name string
		out  string
		want float64
		ok   bool
	}{
		{"happy path", "Weighted total: 0.889  [Phase 4]", 0.889, true},
		{"first match wins", "noise\nWeighted total: 0.700\nWeighted total: 0.800\n", 0.700, true},
		{"missing", "Suite: reflib\nNDCG@10: 0.95", 0, false},
		{"malformed value", "Weighted total: not-a-number", 0, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := parseWeightedTotal(tc.out)
			if ok != tc.ok {
				t.Fatalf("ok: got %v, want %v", ok, tc.ok)
			}
			if ok && got != tc.want {
				t.Errorf("got %v, want %v", got, tc.want)
			}
		})
	}
}

func TestTruncate(t *testing.T) {
	if got := truncate([]byte("short"), 10); got != "short" {
		t.Errorf("under-limit: got %q", got)
	}
	if got := truncate([]byte("0123456789ABCDEF"), 8); !strings.HasSuffix(got, "...[truncated]") {
		t.Errorf("over-limit suffix: got %q", got)
	}
}
