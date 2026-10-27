package api

import (
	"errors"
	"testing"
	"time"
)

// A single unverifiable row must never block its device beyond the grace window.
func TestInboxVerdictFor(t *testing.T) {
	for _, tc := range []struct {
		name string
		age  time.Duration
		want inboxVerdict
	}{
		{"just arrived", 0, inboxRetry},
		{"inside grace", inboxVerifyGrace - time.Second, inboxRetry},
		{"at boundary", inboxVerifyGrace, inboxSkip},
		{"long past grace", 26 * time.Hour, inboxSkip},
		{"future clock skew", -2 * time.Second, inboxRetry},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := inboxVerdictFor(tc.age); got != tc.want {
				t.Fatalf("inboxVerdictFor(%v) = %v, want %v", tc.age, got, tc.want)
			}
		})
	}
}

func TestInboxVerifyGraceStaysBounded(t *testing.T) {
	if inboxVerifyGrace < time.Second {
		t.Fatalf("grace %v is too short for TDengine batch writes", inboxVerifyGrace)
	}
	if inboxVerifyGrace > 10*time.Minute {
		t.Fatalf("grace %v would let one row hold a device too long", inboxVerifyGrace)
	}
	if inboxTickBudget <= 0 || inboxTickBudget > 2*time.Minute {
		t.Fatalf("tick budget %v is outside the intended range", inboxTickBudget)
	}
}

func TestFirstErrorKeepsEarliest(t *testing.T) {
	first, second := errors.New("first"), errors.New("second")
	if got := firstError(nil, nil); got != nil {
		t.Fatalf("firstError(nil, nil) = %v, want nil", got)
	}
	if got := firstError(nil, second); got != second {
		t.Fatalf("firstError(nil, second) = %v, want second", got)
	}
	if got := firstError(first, second); got != first {
		t.Fatalf("firstError(first, second) = %v, want first", got)
	}
}
