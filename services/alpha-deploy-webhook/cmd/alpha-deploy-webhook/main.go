// Package main is the kairix alpha-deploy-webhook entrypoint.
//
// Responds to a signed POST from GitHub Actions (release-vm-deploy.yml),
// pulls the alpha Docker image, runs `kairix onboard check --json` plus
// `kairix benchmark run --suite reflib`, and posts the result back as
// a GitHub commit status on the alpha tag's SHA.
//
// Configuration is environment-driven; secrets live in files (see
// services/alpha-deploy-webhook/README.md §Configuration). Logs go to
// stderr as JSON via log/slog (G8). Bound to `WEBHOOK_LISTEN` (default
// `:9443`) — typically fronted by cloudflared on the VM.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/config"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/deploy"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/handler"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/status"
)

// version is overridden at build time via -ldflags. Defaults to "dev"
// for local builds. (G1: every kairix Go binary exposes --version.)
var version = "dev"

func main() {
	exitCode := run(os.Args[1:], os.Stdout, os.Stderr)
	os.Exit(exitCode)
}

// run is the testable entrypoint. argv excludes os.Args[0]; stdout
// receives normal output; stderr receives logs.
func run(argv []string, stdout, stderr io.Writer) int {
	fs := flag.NewFlagSet("alpha-deploy-webhook", flag.ContinueOnError)
	fs.SetOutput(stderr)
	showVersion := fs.Bool("version", false, "print version and exit (G1)")
	if err := fs.Parse(argv); err != nil {
		return 2
	}
	if *showVersion {
		if _, err := fmt.Fprintf(stdout, "alpha-deploy-webhook %s\n", version); err != nil {
			return 1
		}
		return 0
	}

	logger := slog.New(slog.NewJSONHandler(stderr, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	cfg, err := config.LoadFromEnv()
	if err != nil {
		logger.Error("config load failed", slog.String("err", err.Error()))
		if errors.Is(err, config.ErrMissingRequired) {
			return 2 // operator misconfiguration — distinct from generic failure
		}
		return 1
	}
	logger.Info("config loaded",
		slog.String("listen", cfg.Listen),
		slog.String("repo", cfg.Repo),
		slog.String("compose_dir", cfg.ComposeDir),
		slog.String("benchmark_suite", cfg.BenchmarkSuite),
		slog.Float64("regression_tolerance", cfg.RegressionTolerance),
		slog.String("version", version),
	)

	deployer := &deploy.Service{
		Runner:              deploy.ExecRunner{},
		ComposeDir:          cfg.ComposeDir,
		BenchmarkSuite:      cfg.BenchmarkSuite,
		RegressionTolerance: cfg.RegressionTolerance,
		Logger:              logger.With(slog.String("component", "deploy")),
	}
	poster := status.NewHTTPPoster(cfg.Repo, cfg.GitHubPAT)

	h := &handler.Handler{
		Secret:        cfg.Secret,
		Deployer:      deployer,
		Poster:        poster,
		Logger:        logger.With(slog.String("component", "handler")),
		DeployTimeout: 10 * time.Minute,
	}

	srv := &http.Server{
		Addr:              cfg.Listen,
		Handler:           h,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	idleConnsClosed := make(chan struct{})
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		logger.Info("shutdown signal received, draining")
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			logger.Error("http shutdown error", slog.String("err", err.Error()))
		}
		h.WaitForInflight()
		close(idleConnsClosed)
	}()

	logger.Info("listening", slog.String("addr", cfg.Listen))
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("listen failed", slog.String("err", err.Error()))
		return 1
	}
	<-idleConnsClosed
	logger.Info("stopped")
	return 0
}
