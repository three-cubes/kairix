// Package signature verifies HMAC-SHA256 request signatures for the
// alpha-deploy webhook. The expected header format mirrors GitHub's
// webhook convention: “X-Kairix-Signature: sha256=<hex>“.
//
// Constant-time comparison is mandatory — a timing leak here would
// let an attacker brute-force the secret byte-by-byte. “crypto/subtle“
// is the canonical primitive; we wrap it for a typed API.
package signature

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"strings"
)

// Errors returned by Verify. Callers can use errors.Is to distinguish
// missing-header (operator-friendly 400) from mismatch (security-relevant 401).
var (
	ErrMissingHeader = errors.New("signature header is empty")
	ErrBadFormat     = errors.New("signature header missing sha256= prefix or hex encoding")
	ErrMismatch      = errors.New("signature does not match expected HMAC-SHA256 of body")
)

// Verify confirms that header is a valid HMAC-SHA256 of body using secret.
// Returns nil on match; one of the package errors otherwise.
//
// header is expected in the form “sha256=<64-hex-chars>“.
//
// ctx is accepted for future signature-cache lookups (G4 compliance);
// the current implementation completes synchronously and ignores it.
func Verify(ctx context.Context, secret, body []byte, header string) error {
	_ = ctx
	if header == "" {
		return ErrMissingHeader
	}
	const prefix = "sha256="
	if !strings.HasPrefix(header, prefix) {
		return ErrBadFormat
	}
	want, err := hex.DecodeString(header[len(prefix):])
	if err != nil {
		return ErrBadFormat
	}
	if len(want) != sha256.Size {
		return ErrBadFormat
	}
	mac := hmac.New(sha256.New, secret)
	if _, err := mac.Write(body); err != nil {
		// hmac.Hash never errors; defensive only.
		return ErrMismatch
	}
	got := mac.Sum(nil)
	if subtle.ConstantTimeCompare(got, want) != 1 {
		return ErrMismatch
	}
	return nil
}

// Sum computes the HMAC-SHA256 of body and returns the canonical
// “sha256=<hex>“ header value. Used by test fixtures to construct
// valid signatures; not called in the request-handling path.
func Sum(secret, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	_, _ = mac.Write(body)
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}
