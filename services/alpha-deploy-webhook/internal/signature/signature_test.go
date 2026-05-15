package signature

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func TestVerify(t *testing.T) {
	secret := []byte("test-secret-not-real")
	body := []byte(`{"version":"v2026.5.15a1","sha":"abc","callback_run_id":"1"}`)
	validHeader := Sum(secret, body)

	tests := []struct {
		name    string
		header  string
		body    []byte
		secret  []byte
		wantErr error
	}{
		{
			name:    "valid signature passes",
			header:  validHeader,
			body:    body,
			secret:  secret,
			wantErr: nil,
		},
		{
			name:    "empty header",
			header:  "",
			body:    body,
			secret:  secret,
			wantErr: ErrMissingHeader,
		},
		{
			name:    "missing sha256= prefix",
			header:  "md5=" + strings.TrimPrefix(validHeader, "sha256="),
			body:    body,
			secret:  secret,
			wantErr: ErrBadFormat,
		},
		{
			name:    "non-hex characters in signature",
			header:  "sha256=zzzz",
			body:    body,
			secret:  secret,
			wantErr: ErrBadFormat,
		},
		{
			name:    "wrong-length hex",
			header:  "sha256=abcd",
			body:    body,
			secret:  secret,
			wantErr: ErrBadFormat,
		},
		{
			name:    "wrong secret",
			header:  validHeader,
			body:    body,
			secret:  []byte("wrong-secret"),
			wantErr: ErrMismatch,
		},
		{
			name:    "tampered body",
			header:  validHeader,
			body:    []byte(`{"version":"v9999.9.9a9","sha":"abc","callback_run_id":"1"}`),
			secret:  secret,
			wantErr: ErrMismatch,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := Verify(context.Background(), tc.secret, tc.body, tc.header)
			if !errors.Is(err, tc.wantErr) {
				t.Errorf("Verify err: got %v, want %v", err, tc.wantErr)
			}
		})
	}
}

// TestSumIsDeterministic pins that Sum produces stable output across
// invocations — the signature header construction itself is the
// canonical form GitHub Actions builds. Sabotage-proof: changing
// the hash algorithm or the prefix would fail this.
func TestSumIsDeterministic(t *testing.T) {
	a := Sum([]byte("k"), []byte("payload"))
	b := Sum([]byte("k"), []byte("payload"))
	if a != b {
		t.Errorf("Sum non-deterministic: %s != %s", a, b)
	}
	if !strings.HasPrefix(a, "sha256=") {
		t.Errorf("Sum prefix: got %q, want sha256=...", a)
	}
	// 7 chars of "sha256=" + 64 hex chars = 71
	if len(a) != 71 {
		t.Errorf("Sum length: got %d, want 71 (sha256= + 64 hex)", len(a))
	}
}
