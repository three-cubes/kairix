package handler

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/deploy"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/signature"
	"github.com/three-cubes/kairix/services/alpha-deploy-webhook/internal/status"
)

type fakeDeployer struct {
	wantVersion string
	result      deploy.Result
	called      int32
}

func (f *fakeDeployer) Run(_ context.Context, version string) deploy.Result {
	f.called++
	f.wantVersion = version
	return f.result
}

type fakePoster struct {
	mu    sync.Mutex
	posts []postRecord
	err   error
}

type postRecord struct {
	SHA   string
	State status.State
	Desc  string
}

func (p *fakePoster) Post(_ context.Context, sha string, state status.State, desc string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.posts = append(p.posts, postRecord{sha, state, desc})
	return p.err
}

func (p *fakePoster) all() []postRecord {
	p.mu.Lock()
	defer p.mu.Unlock()
	c := make([]postRecord, len(p.posts))
	copy(c, p.posts)
	return c
}

func newHandler(secret []byte, d Deployer, p status.Poster) *Handler {
	return &Handler{
		Secret:        secret,
		Deployer:      d,
		Poster:        p,
		Logger:        slog.New(slog.NewJSONHandler(io.Discard, nil)),
		DeployTimeout: 30 * time.Second,
	}
}

func TestServeHTTPHealthz(t *testing.T) {
	h := newHandler([]byte("s"), &fakeDeployer{}, &fakePoster{})
	srv := httptest.NewServer(h)
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/healthz")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("status: got %d, want 200", resp.StatusCode)
	}
	body, _ := io.ReadAll(resp.Body)
	if !strings.Contains(string(body), `"ok":true`) {
		t.Errorf("body: got %q", string(body))
	}
}

func TestServeHTTPUnknownPath(t *testing.T) {
	h := newHandler([]byte("s"), &fakeDeployer{}, &fakePoster{})
	srv := httptest.NewServer(h)
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/no-such-endpoint")
	if err != nil {
		t.Fatalf("Get: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("status: got %d, want 404", resp.StatusCode)
	}
}

func TestDeploySuccessPath(t *testing.T) {
	secret := []byte("test-secret")
	body := []byte(`{"version":"v2026.5.15a1","sha":"abc","callback_run_id":"42"}`)
	d := &fakeDeployer{result: deploy.Result{Success: true, Summary: "alpha v2026.5.15a1 validated", WeightedTotal: 0.889}}
	p := &fakePoster{}
	h := newHandler(secret, d, p)
	srv := httptest.NewServer(h)
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", strings.NewReader(string(body)))
	req.Header.Set(SignatureHeader, signature.Sum(secret, body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusAccepted {
		t.Errorf("status: got %d, want 202", resp.StatusCode)
	}
	h.WaitForInflight()

	if d.called != 1 {
		t.Errorf("Deployer.Run called %d times, want 1", d.called)
	}
	if d.wantVersion != "v2026.5.15a1" {
		t.Errorf("Deployer.Run version: got %q", d.wantVersion)
	}
	posts := p.all()
	if len(posts) != 2 {
		t.Fatalf("posts: got %d, want 2 (pending + final)", len(posts))
	}
	if posts[0].State != status.StatePending {
		t.Errorf("first post state: got %q, want pending", posts[0].State)
	}
	if posts[1].State != status.StateSuccess {
		t.Errorf("second post state: got %q, want success", posts[1].State)
	}
	if posts[1].SHA != "abc" {
		t.Errorf("second post sha: got %q, want abc", posts[1].SHA)
	}
}

func TestDeployFailureRoutesAsFailure(t *testing.T) {
	secret := []byte("s")
	body := []byte(`{"version":"v2026.5.15a1","sha":"abc","callback_run_id":"42"}`)
	d := &fakeDeployer{result: deploy.Result{Success: false, Summary: "onboard check failed"}}
	p := &fakePoster{}
	h := newHandler(secret, d, p)
	srv := httptest.NewServer(h)
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", strings.NewReader(string(body)))
	req.Header.Set(SignatureHeader, signature.Sum(secret, body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()
	h.WaitForInflight()

	posts := p.all()
	if posts[1].State != status.StateFailure {
		t.Errorf("final state on deploy failure: got %q, want failure", posts[1].State)
	}
}

func TestDeployBadSignature(t *testing.T) {
	d := &fakeDeployer{}
	p := &fakePoster{}
	h := newHandler([]byte("real-secret"), d, p)
	srv := httptest.NewServer(h)
	defer srv.Close()

	body := []byte(`{"version":"v2026.5.15a1","sha":"abc"}`)
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", strings.NewReader(string(body)))
	req.Header.Set(SignatureHeader, signature.Sum([]byte("wrong-secret"), body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusUnauthorized {
		t.Errorf("status: got %d, want 401", resp.StatusCode)
	}
	h.WaitForInflight()
	if d.called != 0 {
		t.Errorf("deployer called %d times on bad-sig, want 0", d.called)
	}
	if len(p.all()) != 0 {
		t.Errorf("status posted on bad-sig, want zero posts")
	}
}

func TestDeployMissingSignatureHeader(t *testing.T) {
	h := newHandler([]byte("s"), &fakeDeployer{}, &fakePoster{})
	srv := httptest.NewServer(h)
	defer srv.Close()

	body := strings.NewReader(`{"version":"v","sha":"s"}`)
	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", body)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status: got %d, want 400 (missing header = bad-format)", resp.StatusCode)
	}
}

func TestDeployBadJSON(t *testing.T) {
	secret := []byte("s")
	body := []byte(`not json`)
	d := &fakeDeployer{}
	h := newHandler(secret, d, &fakePoster{})
	srv := httptest.NewServer(h)
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", strings.NewReader(string(body)))
	req.Header.Set(SignatureHeader, signature.Sum(secret, body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status: got %d, want 400", resp.StatusCode)
	}
	if d.called != 0 {
		t.Errorf("deployer called on bad-json, want 0")
	}
}

func TestDeployMissingRequiredFields(t *testing.T) {
	secret := []byte("s")
	body := []byte(`{"callback_run_id":"42"}`)
	d := &fakeDeployer{}
	h := newHandler(secret, d, &fakePoster{})
	srv := httptest.NewServer(h)
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/deploy", strings.NewReader(string(body)))
	req.Header.Set(SignatureHeader, signature.Sum(secret, body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("Do: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusBadRequest {
		t.Errorf("status: got %d, want 400 (missing version/sha)", resp.StatusCode)
	}
	if d.called != 0 {
		t.Errorf("deployer called with empty version, want 0")
	}
}
