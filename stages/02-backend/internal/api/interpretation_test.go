package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"powersystem/backend/internal/config"
	"testing"
)

func TestDisabledAgentDoesNotConnect(t *testing.T) {
	calls := 0
	endpoint := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
	defer endpoint.Close()
	for _, cfg := range []config.Config{{AgentURL: endpoint.URL}, {AgentEnabled: true, AgentURL: endpoint.URL}} {
		server := &Server{cfg: cfg}
		_, err := server.callAgent(context.Background(), http.MethodPost, "/internal/agent/interpret", nil)
		if err == nil || err.Error() != "not_configured" || retryInterpretation(err.Error(), 1) {
			t.Fatal("disabled/missing service credential must degrade without retry")
		}
	}
	if calls != 0 {
		t.Fatal("disabled service received a request")
	}
}

func TestInterpretationRetryPolicy(t *testing.T) {
	for _, reason := range []string{"not_configured", "auth_failed", "quota_exhausted", "answer_validation_failed", "missing_event_evidence"} {
		if retryInterpretation(reason, 1) {
			t.Fatalf("permanent failure retried: %s", reason)
		}
	}
	for _, reason := range []string{"rate_limited", "unavailable"} {
		if !retryInterpretation(reason, 1) || !retryInterpretation(reason, 2) || retryInterpretation(reason, 3) {
			t.Fatalf("invalid bounded retry: %s", reason)
		}
	}
}
