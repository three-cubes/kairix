package status

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// captureHandler records the most recent request — used to assert the
// shape of the POST we send to GitHub. Each test gets its own instance.
type captureHandler struct {
	gotMethod string
	gotPath   string
	gotAuth   string
	gotAPIVer string
	gotBody   statusBody
	respCode  int
	respBody  string
}

func (h *captureHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.gotMethod = r.Method
	h.gotPath = r.URL.Path
	h.gotAuth = r.Header.Get("Authorization")
	h.gotAPIVer = r.Header.Get("X-GitHub-Api-Version")
	raw, _ := io.ReadAll(r.Body)
	_ = json.Unmarshal(raw, &h.gotBody)
	if h.respCode == 0 {
		h.respCode = 201
	}
	w.WriteHeader(h.respCode)
	if h.respBody != "" {
		_, _ = w.Write([]byte(h.respBody))
	}
}

// newTestPoster builds an HTTPPoster pointed at a local httptest server.
// We rewrite the api.github.com URL by intercepting at the http.Client's
// Transport rather than swapping the URL — keeps the production code path
// (URL construction) identical to what runs in prod.
func newTestPoster(repo, pat string, h *captureHandler) (*HTTPPoster, *httptest.Server) {
	srv := httptest.NewServer(h)
	p := NewHTTPPoster(repo, pat)
	p.Client = srv.Client()
	p.Client.Transport = redirectTransport{base: srv.URL, inner: srv.Client().Transport}
	return p, srv
}

// redirectTransport rewrites api.github.com requests to the local test
// server while preserving the production URL-construction path.
type redirectTransport struct {
	base  string
	inner http.RoundTripper
}

func (t redirectTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	// Replace scheme+host with the test server's.
	req.URL.Scheme = "http"
	req.URL.Host = strings.TrimPrefix(t.base, "http://")
	if t.inner == nil {
		return http.DefaultTransport.RoundTrip(req)
	}
	return t.inner.RoundTrip(req)
}

func TestHTTPPosterPostSuccess(t *testing.T) {
	h := &captureHandler{respCode: 201}
	p, srv := newTestPoster("three-cubes/kairix", "ghp_test", h)
	defer srv.Close()

	err := p.Post(context.Background(), "abc123", StateSuccess, "reflib regression OK")
	if err != nil {
		t.Fatalf("Post: %v", err)
	}
	if h.gotMethod != http.MethodPost {
		t.Errorf("method: got %q, want POST", h.gotMethod)
	}
	if h.gotPath != "/repos/three-cubes/kairix/statuses/abc123" {
		t.Errorf("path: got %q", h.gotPath)
	}
	if h.gotAuth != "Bearer ghp_test" {
		t.Errorf("Authorization: got %q", h.gotAuth)
	}
	if h.gotAPIVer != "2022-11-28" {
		t.Errorf("X-GitHub-Api-Version: got %q", h.gotAPIVer)
	}
	if h.gotBody.State != "success" {
		t.Errorf("state: got %q, want success", h.gotBody.State)
	}
	if h.gotBody.Context != Context {
		t.Errorf("context: got %q, want %q", h.gotBody.Context, Context)
	}
	if h.gotBody.Description != "reflib regression OK" {
		t.Errorf("description: got %q", h.gotBody.Description)
	}
}

func TestHTTPPosterTruncatesLongDescription(t *testing.T) {
	h := &captureHandler{respCode: 201}
	p, srv := newTestPoster("o/r", "t", h)
	defer srv.Close()

	long := strings.Repeat("x", 250)
	if err := p.Post(context.Background(), "sha", StateFailure, long); err != nil {
		t.Fatalf("Post: %v", err)
	}
	if got := h.gotBody.Description; len(got) != 140 {
		t.Errorf("description length: got %d, want 140 (GitHub API limit)", len(got))
	}
}

func TestHTTPPosterPropagatesNon2xx(t *testing.T) {
	h := &captureHandler{respCode: 422, respBody: `{"message":"Validation Failed"}`}
	p, srv := newTestPoster("o/r", "t", h)
	defer srv.Close()

	err := p.Post(context.Background(), "sha", StatePending, "x")
	if err == nil {
		t.Fatal("expected error on 422, got nil")
	}
	if !strings.Contains(err.Error(), "422") || !strings.Contains(err.Error(), "Validation Failed") {
		t.Errorf("error message: got %q, want 422 + 'Validation Failed' substring", err)
	}
}

func TestStateConstants(t *testing.T) {
	// Sabotage-proof: GitHub's API rejects anything not in this set.
	// Renaming a State value silently could pass other tests but break
	// production once the API call lands.
	if string(StateSuccess) != "success" || string(StateFailure) != "failure" ||
		string(StatePending) != "pending" || string(StateError) != "error" {
		t.Errorf("state constants drifted from GitHub API: %s %s %s %s",
			StateSuccess, StateFailure, StatePending, StateError)
	}
}
