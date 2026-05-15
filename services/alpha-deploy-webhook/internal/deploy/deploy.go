// Package deploy orchestrates the VM-side alpha-deploy sequence:
// docker compose pull → up -d → kairix onboard check → benchmark run.
//
// Every external command goes through the Runner interface so the
// handler layer can inject a fake. The production Runner shells out via
// os/exec.CommandContext with the request context propagated through.
package deploy

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os/exec"
	"strings"
)

// Runner runs operator-side commands. Production: ExecRunner.
// Tests: fake implementation returning fixed stdout/exit.
type Runner interface {
	// Run executes cmd with args, returning combined stdout+stderr and
	// an error if the command exited non-zero or could not be started.
	// The ctx must be respected so a hung command is killable.
	Run(ctx context.Context, dir, cmd string, args ...string) ([]byte, error)
}

// ExecRunner is the production Runner. Each call uses
// exec.CommandContext so ctx cancellation propagates to the subprocess.
type ExecRunner struct{}

// Run shells out to cmd with args from dir, capturing combined output.
func (ExecRunner) Run(ctx context.Context, dir, cmd string, args ...string) ([]byte, error) {
	c := exec.CommandContext(ctx, cmd, args...) // #nosec G204 — cmd is hardcoded constant in Service; args are derived from validated config + version, not free-form user input.
	c.Dir = dir
	out, err := c.CombinedOutput()
	if err != nil {
		return out, fmt.Errorf("exec %s %v: %w", cmd, args, err)
	}
	return out, nil
}

// Result is the outcome of a Deploy run, used by the handler to drive
// the GitHub commit-status POST.
type Result struct {
	// Success is true only when every step (pull, up, onboard, benchmark) passed.
	Success bool
	// Summary is a short operator-facing line — goes verbatim into the
	// commit-status description field (140-char GitHub limit applies).
	Summary string
	// Details carries the longer log lines for debugging; not posted
	// to GitHub, captured in handler logs.
	Details string
	// WeightedTotal is the benchmark suite's weighted_total metric;
	// 0 when the benchmark step did not complete.
	WeightedTotal float64
}

// Service composes the deploy steps. Inject a fake Runner for tests.
type Service struct {
	Runner                Runner
	ComposeDir            string
	BenchmarkSuite        string
	RegressionTolerance   float64
	BaselineWeightedTotal float64
	Logger                *slog.Logger
}

// Run executes the full deploy sequence. Never panics; every failure
// is captured in Result.Success=false with an operator-readable Summary.
func (s *Service) Run(ctx context.Context, version string) Result {
	if err := s.pullAndUp(ctx, version); err != nil {
		return Result{Success: false, Summary: "docker pull/up failed", Details: err.Error()}
	}
	if err := s.onboardCheck(ctx); err != nil {
		return Result{Success: false, Summary: "onboard check failed", Details: err.Error()}
	}
	wt, err := s.benchmark(ctx)
	if err != nil {
		return Result{Success: false, Summary: "benchmark failed", Details: err.Error()}
	}
	if s.BaselineWeightedTotal > 0 {
		delta := s.BaselineWeightedTotal - wt
		if delta > s.RegressionTolerance {
			return Result{
				Success:       false,
				Summary:       fmt.Sprintf("regression: weighted=%.4f vs baseline=%.4f (delta=%.4f > tolerance=%.4f)", wt, s.BaselineWeightedTotal, delta, s.RegressionTolerance),
				WeightedTotal: wt,
			}
		}
	}
	return Result{
		Success:       true,
		Summary:       fmt.Sprintf("alpha %s validated — weighted=%.4f", version, wt),
		WeightedTotal: wt,
	}
}

func (s *Service) pullAndUp(ctx context.Context, version string) error {
	s.Logger.Info("docker compose pull", slog.String("version", version), slog.String("dir", s.ComposeDir))
	out, err := s.Runner.Run(ctx, s.ComposeDir, "docker", "compose", "pull", "kairix", "kairix-worker")
	if err != nil {
		return fmt.Errorf("pull: %w (output: %s)", err, truncate(out, 500))
	}
	s.Logger.Info("docker compose up -d")
	out, err = s.Runner.Run(ctx, s.ComposeDir, "docker", "compose", "up", "-d", "kairix", "kairix-worker")
	if err != nil {
		return fmt.Errorf("up: %w (output: %s)", err, truncate(out, 500))
	}
	return nil
}

func (s *Service) onboardCheck(ctx context.Context) error {
	s.Logger.Info("kairix onboard check")
	out, err := s.Runner.Run(ctx, s.ComposeDir, "docker", "exec", "app-kairix-1", "kairix", "onboard", "check", "--json")
	if err != nil {
		return fmt.Errorf("onboard exec: %w (output: %s)", err, truncate(out, 500))
	}
	var payload struct {
		FullyPassed bool `json:"fully_passed"`
		Passed      int  `json:"passed"`
		Total       int  `json:"total"`
	}
	if err := json.Unmarshal(out, &payload); err != nil {
		return fmt.Errorf("onboard json parse: %w (output: %s)", err, truncate(out, 200))
	}
	if !payload.FullyPassed {
		return fmt.Errorf("onboard %d/%d (not fully_passed)", payload.Passed, payload.Total)
	}
	return nil
}

// errBenchmarkParse signals the benchmark output couldn't be parsed for
// a weighted_total. Distinct so the handler logs it separately.
var errBenchmarkParse = errors.New("benchmark output missing weighted_total")

func (s *Service) benchmark(ctx context.Context) (float64, error) {
	s.Logger.Info("kairix benchmark run", slog.String("suite", s.BenchmarkSuite))
	out, err := s.Runner.Run(ctx, s.ComposeDir, "docker", "exec", "app-kairix-1",
		"sh", "-c", fmt.Sprintf("cd /opt/kairix && kairix benchmark run --suite %s", s.BenchmarkSuite))
	if err != nil {
		return 0, fmt.Errorf("benchmark exec: %w (output: %s)", err, truncate(out, 500))
	}
	wt, ok := parseWeightedTotal(string(out))
	if !ok {
		return 0, fmt.Errorf("%w (last output: %s)", errBenchmarkParse, truncate(out, 200))
	}
	return wt, nil
}

// parseWeightedTotal scans benchmark stdout for a "Weighted total: X.XXX"
// line and returns the float. The benchmark CLI emits a stable line
// format; we deliberately parse text rather than JSON because the
// current CLI doesn't emit JSON for the suite run.
func parseWeightedTotal(out string) (float64, bool) {
	for _, line := range strings.Split(out, "\n") {
		if !strings.Contains(line, "Weighted total:") {
			continue
		}
		fields := strings.Fields(line)
		// Expect: "Weighted" "total:" "0.882" [...rest...]
		if len(fields) < 3 {
			continue
		}
		var f float64
		if _, err := fmt.Sscanf(fields[2], "%f", &f); err == nil {
			return f, true
		}
	}
	return 0, false
}

func truncate(b []byte, n int) string {
	if len(b) <= n {
		return string(b)
	}
	return string(b[:n]) + "...[truncated]"
}
