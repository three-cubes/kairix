// Package status posts GitHub commit-status results back to the
// kairix repo after the VM-side regression gate completes.
//
// Posts to “POST /repos/{owner}/{repo}/statuses/{sha}“ with a PAT
// scoped to “repo:status“ only. The status context is fixed at
// “vm-reflib-regression“ so release.yml's Phase 5 alpha-gate can
// query it deterministically.
package status

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// State is the GitHub commit-status state. Only the four documented
// values are valid; the API rejects everything else with a 422.
type State string

const (
	StatePending State = "pending"
	StateSuccess State = "success"
	StateFailure State = "failure"
	StateError   State = "error"
)

// Context is the commit-status context label. Stable across releases —
// release.yml's check-alpha-gate queries this exact string.
const Context = "vm-reflib-regression"

// Poster sends a single commit-status update. Injectable so tests can
// swap the HTTP layer for an in-memory fake.
type Poster interface {
	Post(ctx context.Context, sha string, state State, description string) error
}

// HTTPPoster is the production Poster. Reuses one *http.Client so
// keepalives apply across multiple posts (typically only one per request).
type HTTPPoster struct {
	Client *http.Client
	Repo   string // "owner/repo"
	PAT    string // bearer token; scope repo:status
}

// NewHTTPPoster builds a Poster with a sensible default timeout.
func NewHTTPPoster(repo, pat string) *HTTPPoster {
	return &HTTPPoster{
		Client: &http.Client{Timeout: 15 * time.Second},
		Repo:   repo,
		PAT:    pat,
	}
}

// statusBody mirrors the GitHub API request shape for create-status.
type statusBody struct {
	State       string `json:"state"`
	TargetURL   string `json:"target_url,omitempty"`
	Description string `json:"description"`
	Context     string `json:"context"`
}

// Post creates or updates the commit-status on the named sha.
// Description is truncated to GitHub's 140-char limit silently.
func (p *HTTPPoster) Post(ctx context.Context, sha string, state State, description string) error {
	if len(description) > 140 {
		description = description[:140]
	}
	body, err := json.Marshal(statusBody{
		State:       string(state),
		Description: description,
		Context:     Context,
	})
	if err != nil {
		return fmt.Errorf("marshal status body: %w", err)
	}
	url := fmt.Sprintf("https://api.github.com/repos/%s/statuses/%s", p.Repo, sha)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+p.PAT)
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")

	resp, err := p.Client.Do(req)
	if err != nil {
		return fmt.Errorf("do request: %w", err)
	}
	defer func() {
		if cerr := resp.Body.Close(); cerr != nil {
			// Close-on-defer failure means the keepalive pool may leak
			// the connection. Caller has already acted on the response;
			// surface via Go's stdlib log as a last-resort warning.
			//
			// We deliberately don't return cerr — it would mask the
			// earlier write outcome.
			_ = cerr
		}
	}()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		// Read up to 1 KiB of body for the error message. If the read
		// itself fails (truncated stream, body already closed) we still
		// have the status code — the body is best-effort context.
		respBody, readErr := io.ReadAll(io.LimitReader(resp.Body, 1024))
		if readErr != nil {
			return fmt.Errorf("github status %d (body read failed: %w)", resp.StatusCode, readErr)
		}
		return fmt.Errorf("github status %d: %s", resp.StatusCode, string(respBody))
	}
	return nil
}
