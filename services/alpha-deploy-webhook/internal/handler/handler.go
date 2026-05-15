// Package handler is the HTTP layer of the alpha-deploy webhook.
// Receives signed POST requests from GitHub Actions, verifies the
// HMAC, kicks off the deploy in a background goroutine, returns
// 202 Accepted immediately, and reports the deploy result back via
// the status.Poster.
//
// The async pattern is deliberate: the benchmark step takes ~5 min,
// far longer than any reasonable HTTP timeout. We accept the request,
// log a correlation id, and let the GitHub-side workflow poll the
// commit status for completion.
package handler

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"sync"
	"time"

	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/deploy"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/signature"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/status"
)

// SignatureHeader is the canonical HMAC header name.
const SignatureHeader = "X-Kairix-Signature"

// DeployRequest is the JSON body posted by GitHub Actions.
type DeployRequest struct {
	Version       string `json:"version"`
	SHA           string `json:"sha"`
	CallbackRunID string `json:"callback_run_id"`
}

// Deployer is the deploy.Service surface the handler depends on; tests
// inject a fake to avoid spawning real docker commands.
type Deployer interface {
	Run(ctx context.Context, version string) deploy.Result
}

// Handler ties config + signature verification + dispatch together.
// Constructed once at boot; every HTTP request reuses the same instance.
type Handler struct {
	Secret   []byte
	Deployer Deployer
	Poster   status.Poster
	Logger   *slog.Logger
	// DeployTimeout caps the background deploy goroutine. Surfaces a
	// stuck docker command as a timed-out commit status instead of an
	// orphan goroutine.
	DeployTimeout time.Duration

	// inflight lets tests wait for the async dispatch to finish.
	inflight sync.WaitGroup
}

// ServeHTTP routes by method + path. Only POST /deploy and GET /healthz
// are exposed; everything else returns 404.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/healthz":
		h.healthz(w)
	case r.Method == http.MethodPost && r.URL.Path == "/deploy":
		h.deploy(w, r)
	default:
		http.NotFound(w, r)
	}
}

func (h *Handler) healthz(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	if _, err := w.Write([]byte(`{"ok":true}`)); err != nil {
		h.Logger.Warn("healthz write failed", slog.String("err", err.Error()))
	}
}

func (h *Handler) deploy(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<14)) // 16 KiB cap
	if err != nil {
		h.Logger.Warn("read body failed", slog.String("err", err.Error()))
		http.Error(w, "read body", http.StatusBadRequest)
		return
	}
	if err := signature.Verify(r.Context(), h.Secret, body, r.Header.Get(SignatureHeader)); err != nil {
		h.Logger.Warn("signature verify failed", slog.String("err", err.Error()))
		if errors.Is(err, signature.ErrMissingHeader) || errors.Is(err, signature.ErrBadFormat) {
			http.Error(w, "bad signature", http.StatusBadRequest)
			return
		}
		http.Error(w, "unauthorised", http.StatusUnauthorized)
		return
	}
	var req DeployRequest
	if err := json.Unmarshal(body, &req); err != nil {
		h.Logger.Warn("body parse failed", slog.String("err", err.Error()))
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if req.Version == "" || req.SHA == "" {
		http.Error(w, "missing version or sha", http.StatusBadRequest)
		return
	}

	// Accept synchronously; do the slow work in the background.
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusAccepted)
	if _, err := w.Write([]byte(`{"accepted":true}`)); err != nil {
		h.Logger.Warn("deploy ack write failed", slog.String("err", err.Error()))
	}

	h.inflight.Add(1)
	go func() {
		defer h.inflight.Done()
		h.runDeployAndReport(req)
	}()
}

// runDeployAndReport executes the deploy chain and posts back to GitHub.
// Detached from the request context so the HTTP timeout doesn't cancel
// it; uses h.DeployTimeout as the cap.
func (h *Handler) runDeployAndReport(req DeployRequest) {
	ctx, cancel := context.WithTimeout(context.Background(), h.DeployTimeout)
	defer cancel()

	h.Logger.Info("deploy start",
		slog.String("version", req.Version),
		slog.String("sha", req.SHA),
		slog.String("callback_run_id", req.CallbackRunID),
	)
	if err := h.Poster.Post(ctx, req.SHA, status.StatePending, "alpha "+req.Version+" — deploy in progress"); err != nil {
		h.Logger.Error("post pending failed", slog.String("err", err.Error()))
	}

	result := h.Deployer.Run(ctx, req.Version)
	finalState := status.StateFailure
	if result.Success {
		finalState = status.StateSuccess
	}
	h.Logger.Info("deploy complete",
		slog.Bool("success", result.Success),
		slog.String("summary", result.Summary),
		slog.Float64("weighted_total", result.WeightedTotal),
	)
	if err := h.Poster.Post(ctx, req.SHA, finalState, result.Summary); err != nil {
		h.Logger.Error("post final failed", slog.String("err", err.Error()))
	}
}

// WaitForInflight blocks until every dispatched deploy goroutine has
// completed its commit-status post. Test-only — production never
// shuts down deliberately, systemd just signals SIGTERM.
func (h *Handler) WaitForInflight() {
	h.inflight.Wait()
}
